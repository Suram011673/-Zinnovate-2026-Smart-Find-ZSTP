"""
AI-powered field extraction pipeline: merges classical layout detection, GPT-4o,
and optional Donut (transformers) DocVQA for layout-agnostic documents.

Nothing here is a static navigation order — the decision engine still picks the
next field at runtime from pending + priority + confidence.
"""
from __future__ import annotations

import io
import logging
import os
from typing import Any

import fitz
from PIL import Image

import pdf_processor as pp
from decision_engine import assign_reading_order_priorities, attach_sections_to_fields
from gpt_validator import extract_fields_gpt, match_block_for_value, validate_fields_gpt

logger = logging.getLogger("smart_find")


def _merge_field_dicts(
    baseline: list[dict[str, Any]],
    extra: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge by field_id; keep higher confidence."""
    by_id: dict[str, dict[str, Any]] = {}
    for row in baseline:
        fid = row.get("field_id") or row.get("name")
        if fid:
            by_id[str(fid)] = dict(row)
    for row in extra:
        fid = str(row.get("field_id") or row.get("name") or "")
        if not fid:
            continue
        prev = by_id.get(fid)
        nc = float(row.get("confidence", 0))
        if prev is None or nc > float(prev.get("confidence", 0)):
            merged = dict(prev or {})
            merged.update(row)
            by_id[fid] = merged
    return list(by_id.values())


def try_donut_docvqa_fields(pdf_bytes: bytes, blocks: list[pp.TextBlock]) -> list[dict[str, Any]]:
    """
    Optional: run Donut docvqa on the first page image for targeted questions.
    Called only when upload sets use_transformers=true or SMART_FIND_TRANSFORMERS=1.
    Requires requirements-ml.txt (torch + transformers).
    """
    try:
        import torch
        from transformers import DonutProcessor, VisionEncoderDecoderModel
    except ImportError:
        logger.info("Transformers/torch not installed; skip Donut path.")
        return []

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    if len(doc) < 1:
        doc.close()
        return []
    page = doc[0]
    mat = fitz.Matrix(2, 2)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    image = Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")
    doc.close()

    model_id = os.getenv("SMART_FIND_DONUT_MODEL", "naver-clova-ix/donut-base-finetuned-docvqa")
    try:
        processor = DonutProcessor.from_pretrained(model_id)
        model = VisionEncoderDecoderModel.from_pretrained(model_id)
        device = "cuda" if torch.cuda.is_available() else "cpu"
        model.to(device)
        model.eval()
    except Exception as e:
        logger.warning("Donut model load failed: %s", e)
        return []

    questions = {
        "policy_number": "What is the policy or member or subscriber id number?",
        "full_name": "What is the full name of the member or patient?",
        "date_of_birth": "What is the date of birth?",
    }
    out: list[dict[str, Any]] = []

    for field_id, question in questions.items():
        try:
            pixel_values = processor(image, return_tensors="pt").pixel_values.to(device)
            q = f"<s_docvqa><s_question>{question}</s_question><s_answer>"
            decoder_input_ids = processor.tokenizer(
                q,
                add_special_tokens=False,
                return_tensors="pt",
            ).input_ids.to(device)
            dec = getattr(model, "decoder", model)
            dec_cfg = getattr(dec, "config", None)
            max_len = getattr(dec_cfg, "max_position_embeddings", 512) if dec_cfg else 512
            with torch.no_grad():
                outputs = model.generate(
                    pixel_values,
                    decoder_input_ids=decoder_input_ids,
                    max_length=min(max_len, 512),
                    pad_token_id=processor.tokenizer.pad_token_id,
                    eos_token_id=processor.tokenizer.eos_token_id,
                    use_cache=True,
                    bad_words_ids=[[processor.tokenizer.unk_token_id]],
                )
            seq = processor.batch_decode(outputs, skip_special_tokens=True)[0]
            answer = seq.replace(question, "").strip()
            if not answer or len(answer) < 2:
                continue
            tb = match_block_for_value([b for b in blocks if b.page == 1] or blocks, answer)
            if tb is None:
                tb = match_block_for_value(blocks, answer)
            if tb is None:
                continue
            conf = 0.55
            out.append(
                {
                    "field_id": field_id,
                    "name": field_id,
                    "value": answer[:200],
                    "bbox": list(tb.bbox),
                    "page": tb.page,
                    "confidence": conf,
                    "source": "donut",
                }
            )
        except Exception as e:
            logger.debug("Donut Q %s: %s", field_id, e)
            continue

    return out


def extract_fields_for_pdf(
    pdf_bytes: bytes,
    *,
    use_ocr_fallback: bool = True,
    aggressive_ocr: bool = True,
    handwriting_merge: bool = False,
    dynamic_fields: bool = True,
    use_openai: bool = False,
    use_gpt_validate: bool = False,
    use_transformers: bool | None = None,
) -> tuple[list[pp.TextBlock], list[dict[str, Any]]]:
    """
    Full pipeline: PyMuPDF (+ OCR) → **dynamic** or classical field detect → GPT merge →
    optional Donut → reading-order priorities (dynamic) → sections.
    """
    if use_ocr_fallback:
        blocks = pp.extract_blocks_with_ocr_fallback(
            pdf_bytes,
            aggressive_ocr=aggressive_ocr,
            handwriting_merge=handwriting_merge,
        )
    else:
        blocks = pp.extract_text_blocks_pymupdf(pdf_bytes)

    if dynamic_fields:
        classical = pp.detect_fields_dynamic_from_blocks(blocks)
    else:
        classical = pp.detect_fields_from_blocks(blocks)
    merged = list(classical)

    if use_openai:
        gpt_rows = extract_fields_gpt(blocks)
        merged = _merge_field_dicts(merged, gpt_rows)

    tf = use_transformers
    if tf is None:
        tf = os.getenv("SMART_FIND_TRANSFORMERS", "").lower() in ("1", "true", "yes")
    if tf:
        donut_rows = try_donut_docvqa_fields(pdf_bytes, blocks)
        merged = _merge_field_dicts(merged, donut_rows)

    if use_gpt_validate and merged:
        merged = validate_fields_gpt(merged, blocks)

    if dynamic_fields and merged:
        merged = assign_reading_order_priorities(merged)

    merged = attach_sections_to_fields(merged, blocks)
    return blocks, merged
