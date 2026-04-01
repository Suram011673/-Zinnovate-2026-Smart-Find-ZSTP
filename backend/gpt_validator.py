"""
OpenAI (GPT-4 / GPT-4o) helpers: structured field extraction and optional validation.
Requires OPENAI_API_KEY. Model overridable via OPENAI_MODEL (default gpt-4o-mini).
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from rapidfuzz import fuzz

from pdf_processor import TextBlock

logger = logging.getLogger("smart_find")

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # type: ignore


def _client() -> Any | None:
    if OpenAI is None or not os.getenv("OPENAI_API_KEY", "").strip():
        return None
    return OpenAI()


def _model() -> str:
    return os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip() or "gpt-4o-mini"


def _build_ocr_prompt(blocks: list[TextBlock], max_chars: int = 12000) -> str:
    """Compact layout-aware text for the model (page + bbox + text)."""
    lines: list[str] = []
    for i, b in enumerate(blocks):
        x0, y0, x1, y1 = b.bbox
        line = f"p{b.page} [{x0:.0f},{y0:.0f},{x1:.0f},{y1:.0f}] {b.text[:200]}"
        lines.append(line)
    blob = "\n".join(lines)
    if len(blob) > max_chars:
        blob = blob[:max_chars] + "\n…(truncated)"
    return blob


def _parse_json_array(raw: str) -> list[dict[str, Any]]:
    raw = raw.strip()
    m = re.search(r"\[[\s\S]*\]", raw)
    if m:
        raw = m.group(0)
    data = json.loads(raw)
    if not isinstance(data, list):
        return []
    return [x for x in data if isinstance(x, dict)]


def match_block_for_value(blocks: list[TextBlock], value: str) -> TextBlock | None:
    if not value or not blocks:
        return None
    v = value.strip()[:120]
    best: TextBlock | None = None
    best_score = 0
    for b in blocks:
        if not b.text.strip():
            continue
        s = max(
            fuzz.ratio(v.lower(), b.text.lower()),
            fuzz.partial_ratio(v.lower(), b.text.lower()),
        )
        if v.lower() in b.text.lower():
            s = max(s, 85)
        if s > best_score:
            best_score = s
            best = b
    return best if best_score >= 35 else None


def extract_fields_gpt(blocks: list[TextBlock]) -> list[dict[str, Any]]:
    """
    Ask the model to infer policy_number, full_name, date_of_birth from OCR lines.
    Maps values back to real bboxes via fuzzy alignment to TextBlocks.
    """
    client = _client()
    if not client or not blocks:
        return []

    ocr_blob = _build_ocr_prompt(blocks)
    system = (
        "You are a document field extractor. From OCR lines (format: "
        "pPAGE [x0,y0,x1,y1] text), output ONLY a JSON array of objects. "
        "Each object: field_id (one of: policy_number, full_name, date_of_birth), "
        "value (exact string from the document), confidence (0.0-1.0). "
        "Skip fields you cannot support with the text. No markdown, no prose."
    )
    user = f"OCR layout:\n{ocr_blob}"

    try:
        resp = client.chat.completions.create(
            model=_model(),
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0.1,
            max_tokens=1024,
        )
        content = (resp.choices[0].message.content or "").strip()
        rows = _parse_json_array(content)
    except Exception as e:
        logger.warning("GPT extraction failed: %s", e)
        return []

    out: list[dict[str, Any]] = []
    allowed = {"policy_number", "full_name", "date_of_birth"}
    for row in rows:
        fid = row.get("field_id") or row.get("name")
        if fid not in allowed:
            continue
        val = row.get("value")
        if val is None or (isinstance(val, str) and not val.strip()):
            continue
        conf = float(row.get("confidence", 0.75))
        conf = max(0.0, min(1.0, conf))
        tb = match_block_for_value(blocks, str(val))
        if tb is None:
            continue
        out.append(
            {
                "field_id": fid,
                "name": fid,
                "value": str(val).strip(),
                "bbox": list(tb.bbox),
                "page": tb.page,
                "confidence": round(conf, 3),
                "source": "gpt",
            }
        )
    return out


def validate_fields_gpt(fields: list[dict[str, Any]], blocks: list[TextBlock]) -> list[dict[str, Any]]:
    """
    Optional second pass: ask GPT to flag suspect extractions and suggest corrections.
    """
    client = _client()
    if not client:
        return fields

    summary = json.dumps(
        [{"field_id": f.get("field_id"), "value": f.get("value"), "confidence": f.get("confidence")} for f in fields],
        indent=0,
    )[:6000]
    snippet = _build_ocr_prompt(blocks, max_chars=4000)

    try:
        resp = client.chat.completions.create(
            model=_model(),
            messages=[
                {
                    "role": "system",
                    "content": "Return ONLY JSON array of {field_id, value_ok (bool), suggested_value or null, adjusted_confidence 0-1}. One entry per input field.",
                },
                {"role": "user", "content": f"Fields:\n{summary}\n\nOCR:\n{snippet}"},
            ],
            temperature=0,
            max_tokens=800,
        )
        content = (resp.choices[0].message.content or "").strip()
        fixes = _parse_json_array(content)
    except Exception as e:
        logger.warning("GPT validation failed: %s", e)
        return fields

    fix_by_id = {str(x.get("field_id")): x for x in fixes if x.get("field_id")}
    merged = []
    for f in fields:
        ff = dict(f)
        fid = str(ff.get("field_id", ""))
        fx = fix_by_id.get(fid)
        if not fx:
            merged.append(ff)
            continue
        if fx.get("suggested_value") and not fx.get("value_ok", True):
            ff["value"] = str(fx["suggested_value"]).strip()
            tb = match_block_for_value(blocks, ff["value"])
            if tb:
                ff["bbox"] = list(tb.bbox)
                ff["page"] = tb.page
        if "adjusted_confidence" in fx and fx["adjusted_confidence"] is not None:
            try:
                ff["confidence"] = max(0.0, min(1.0, float(fx["adjusted_confidence"])))
            except (TypeError, ValueError):
                pass
        ff["gpt_validated"] = True
        merged.append(ff)
    return merged
