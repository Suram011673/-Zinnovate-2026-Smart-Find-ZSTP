"""
Dynamic next-field selection: pending only, sort by priority ASC then confidence DESC.
"""
from __future__ import annotations

from typing import Any, Optional


def coerce_int(v: Any, default: int = 1) -> int:
    """Safe int for sort keys / API (None or bad values → default)."""
    if v is None:
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def coerce_float(v: Any, default: float = 0.0) -> float:
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


# Lower number = navigate first
DEFAULT_PRIORITY = {
    "policy_number": 1,
    "full_name": 2,
    "date_of_birth": 3,
    "policy_number_alt": 4,
}


def attach_priorities(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Set priority from known schema unless already set (e.g. dynamic reading order)."""
    for f in fields:
        p = f.get("priority")
        if p is not None:
            try:
                int(p)
                continue
            except (TypeError, ValueError):
                pass
        fid = f.get("field_id") or f.get("name")
        f["priority"] = DEFAULT_PRIORITY.get(str(fid), 99)
    return fields


def assign_reading_order_priorities(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Dynamic navigation order: top-to-bottom, left-to-right per page (visual flow).
    Mutates field dicts in place.
    """
    if not fields:
        return fields

    def sort_key(f: dict[str, Any]) -> tuple[int, float, float]:
        bb = f.get("bbox") or [0.0, 0.0, 0.0, 0.0]
        return (
            coerce_int(f.get("page"), 1),
            float(bb[1]) if len(bb) > 1 else 0.0,
            float(bb[0]) if len(bb) > 0 else 0.0,
        )

    for i, f in enumerate(sorted(fields, key=sort_key), start=1):
        f["priority"] = i
    return fields


def filter_pending(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Only fields still pending and having a detectable value (skip empty / missing)."""
    out = []
    for f in fields:
        if f.get("status") != "pending":
            continue
        val = f.get("value")
        if val is None or (isinstance(val, str) and not val.strip()):
            continue
        if str(val).strip() in ("(see page)",):
            continue
        out.append(f)
    return out


def sort_for_next(fields: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    Priority ascending, confidence descending, then section (groups related layout bands).
    Order is computed at runtime from current pending set — not a fixed script.
    """
    return sorted(
        fields,
        key=lambda f: (
            coerce_int(f.get("priority"), 99),
            -coerce_float(f.get("confidence"), 0.0),
            str(f.get("section") or ""),
        ),
    )


def get_next_field(fields: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """
    Return the next field the user should review, or None if queue empty.
    """
    pending = filter_pending(fields)
    if not pending:
        return None
    ordered = sort_for_next(pending)
    return ordered[0]


def mark_needs_review(field: dict[str, Any], threshold: float = 0.7) -> dict[str, Any]:
    """Flag low-confidence extractions for manual review in the UI."""
    field = dict(field)
    field["needs_review"] = coerce_float(field.get("confidence"), 0.0) < threshold
    return field


def build_page_heights_from_blocks(blocks: list[Any]) -> dict[int, float]:
    """Estimate page height (PDF points) from lowest content — for vertical bands."""
    max_y: dict[int, float] = {}
    for b in blocks:
        p = coerce_int(getattr(b, "page", 1), 1)
        bb = getattr(b, "bbox", (0.0, 0.0, 0.0, 792.0))
        if isinstance(bb, (list, tuple)) and len(bb) >= 4:
            max_y[p] = max(max_y.get(p, 0.0), float(bb[3]))
    if not max_y:
        return {1: 792.0}
    return {p: max(792.0, y * 1.12) for p, y in max_y.items()}


def infer_section(page: int, bbox: list[float] | tuple[float, ...], page_heights: dict[int, float]) -> str:
    """Vertical band on page: header / body / footer — dynamic grouping label."""
    h = float(page_heights.get(page, 792.0))
    if len(bbox) < 4:
        return f"page{page}_unknown"
    cy = (float(bbox[1]) + float(bbox[3])) / 2.0
    ratio = cy / h if h > 0 else 0.5
    if ratio < 0.34:
        band = "header"
    elif ratio < 0.67:
        band = "body"
    else:
        band = "footer"
    return f"page{page}_{band}"


def attach_sections_to_fields(fields: list[dict[str, Any]], blocks: list[Any]) -> list[dict[str, Any]]:
    heights = build_page_heights_from_blocks(blocks)
    for f in fields:
        bbox = f.get("bbox") or [0, 0, 0, 0]
        page = coerce_int(f.get("page"), 1)
        f["section"] = infer_section(page, list(bbox), heights)
    return fields
