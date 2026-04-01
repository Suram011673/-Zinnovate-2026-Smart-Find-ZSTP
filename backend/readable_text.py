"""
Turn OCR blocks into reading-order paragraphs (handwritten / boxed prose).
"""
from __future__ import annotations

import re
from typing import Any

from document_search import group_ocr_blocks_into_lines

# Drop standalone watermark tokens (diagonal EXAMPLE etc.)
_NOISE_ONLY = frozenset(
    {"EXAMPLE", "SAMPLE", "DRAFT", "COPY", "CONFIDENTIAL", "WATERMARK"}
)


def _is_noise_span(text: str) -> bool:
    t = text.strip()
    if not t:
        return True
    u = t.upper().replace(" ", "")
    return u in _NOISE_ONLY and len(t) <= 14


def _filter_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for b in blocks:
        tx = (b.get("text") or "").strip()
        if not tx or _is_noise_span(tx):
            continue
        out.append(b)
    return out


def _line_sort_key(line: list[dict[str, Any]]) -> tuple[int, float]:
    page = int(line[0].get("page", 1))
    ys = [float(b["bbox"][1]) for b in line]
    return (page, sum(ys) / max(len(ys), 1))


def _merge_line_text(line: list[dict[str, Any]]) -> str:
    ordered = sorted(line, key=lambda b: float(b["bbox"][0]))
    parts = [(b.get("text") or "").strip() for b in ordered]
    return " ".join(p for p in parts if p)


def blocks_to_readable_document(
    blocks: list[dict[str, Any]],
    *,
    line_y_tol: float = 22.0,
    line_x_gap: float = 140.0,
    paragraph_gap_pt: float = 26.0,
) -> dict[str, Any]:
    """
    Group OCR boxes into lines, then paragraphs using vertical gaps.
    Returns { pages: [{ page, paragraphs: [str] }], full_text, line_count }.
    """
    clean = _filter_blocks(blocks)
    if not clean:
        return {"pages": [], "full_text": "", "line_count": 0, "paragraph_count": 0}

    lines = group_ocr_blocks_into_lines(
        clean, y_tol=line_y_tol, max_x_gap=line_x_gap
    )
    lines.sort(key=_line_sort_key)

    page_paras: dict[int, list[str]] = {}
    current_page: int | None = None
    current_para_lines: list[str] = []
    prev_bottom: float | None = None
    line_count = 0

    def flush_paragraph() -> None:
        nonlocal current_para_lines, current_page
        if not current_para_lines or current_page is None:
            current_para_lines = []
            return
        para = "\n".join(current_para_lines).strip()
        if para:
            page_paras.setdefault(current_page, []).append(para)
        current_para_lines = []

    for line in lines:
        line_count += 1
        page = int(line[0].get("page", 1))
        merged = _merge_line_text(line)
        if not merged or _is_noise_span(merged):
            continue

        tops = [float(b["bbox"][1]) for b in line]
        bottoms = [float(b["bbox"][3]) for b in line]
        top, bottom = min(tops), max(bottoms)

        if current_page is None:
            current_page = page
        if page != current_page:
            flush_paragraph()
            current_page = page
            prev_bottom = None

        if prev_bottom is not None and top - prev_bottom > paragraph_gap_pt:
            flush_paragraph()

        current_para_lines.append(merged)
        prev_bottom = bottom

    flush_paragraph()

    pages_out = [
        {"page": p, "paragraphs": paras}
        for p, paras in sorted(page_paras.items())
    ]
    parts_full: list[str] = []
    for p in pages_out:
        hdr = f"--- Page {p['page']} ---\n\n"
        body = "\n\n".join(p["paragraphs"])
        parts_full.append(hdr + body)

    full_text = "\n\n".join(parts_full).strip()
    para_n = sum(len(p["paragraphs"]) for p in pages_out)

    return {
        "pages": pages_out,
        "full_text": full_text,
        "line_count": line_count,
        "paragraph_count": para_n,
    }


def strip_duplicate_whitespace(text: str) -> str:
    return re.sub(r"[ \t]+", " ", re.sub(r"\n{3,}", "\n\n", text)).strip()
