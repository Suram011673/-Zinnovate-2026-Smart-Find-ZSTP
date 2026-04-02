"""
Search OCR / native text blocks (handwriting-friendly).
Merges same-line blocks, normalizes text, uses fuzzy match when exact substring fails.
"""
from __future__ import annotations

import math
import os
import re
import unicodedata
from typing import Any

from rapidfuzz import fuzz

# Min query length for fuzzy (avoid noise on "a", "in")
FUZZY_MIN_QUERY_LEN = 3
# Stricter than before — loose fuzzy was matching unrelated OCR lines (false "hits" on some PDFs)
PARTIAL_RATIO_LINE = 88
PARTIAL_RATIO_LINE_MULTIWORD = 92
PARTIAL_RATIO_BLOCK = 90
RATIO_BLOCK = 90
TOKEN_SORT_LINE = 86

# Single-token strings that usually appear as diagonal stamps / watermarks — not body copy.
_STANDALONE_WATERMARK_TOKENS = frozenset(
    {"example", "sample", "draft", "copy", "confidential", "watermark"},
)
# Skip Find hits when the block is only one of those tokens *and* geometry looks like a page-spanning stamp.
_WATERMARKISH_MIN_AREA_PT2 = 12_000.0
_WATERMARKISH_MIN_MAX_SIDE_PT = 220.0


def _bbox_area_pt2(bbox: list[float] | tuple[float, ...]) -> float:
    if not bbox or len(bbox) < 4:
        return 0.0
    x0, y0, x1, y1 = (float(bbox[i]) for i in range(4))
    return max(0.0, abs(x1 - x0)) * max(0.0, abs(y1 - y0))


def _bbox_max_side_pt(bbox: list[float] | tuple[float, ...]) -> float:
    if not bbox or len(bbox) < 4:
        return 0.0
    x0, y0, x1, y1 = (float(bbox[i]) for i in range(4))
    return max(abs(x1 - x0), abs(y1 - y0))


def _skip_watermark_like_hit(block_text: str, bbox: list[float], q_raw: str) -> bool:
    """
    Diagonal OCR often emits one giant box for EXAMPLE/SAMPLE; Find then draws a full-width bar.
    Do not drop real sentences that merely contain the same word — only standalone stamp-sized text.
    Set SMART_FIND_SKIP_WATERMARK_SEARCH=0 to disable this filter.
    """
    raw = (os.environ.get("SMART_FIND_SKIP_WATERMARK_SEARCH", "1") or "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    t = (block_text or "").strip()
    if len(t) > 14:
        return False
    if _norm(t) != _norm(q_raw):
        return False
    if _norm(t) not in _STANDALONE_WATERMARK_TOKENS:
        return False
    a = _bbox_area_pt2(bbox)
    mx = _bbox_max_side_pt(bbox)
    return a >= _WATERMARKISH_MIN_AREA_PT2 or mx >= _WATERMARKISH_MIN_MAX_SIDE_PT


def _line_partial_threshold(q_raw: str, qn: str) -> int:
    """Adaptive threshold: short single-word handwriting queries need looser fuzzy."""
    multi = " " in (q_raw or "").strip()
    if multi:
        return PARTIAL_RATIO_LINE_MULTIWORD
    L = len(qn or "")
    if L <= 5:
        return 80
    if L <= 7:
        return 84
    return PARTIAL_RATIO_LINE


def _block_fuzzy_thresholds(q_raw: str, qn: str) -> tuple[int, int]:
    """
    Adaptive block fuzzy thresholds.
    Keeps strict defaults for longer phrases, but relaxes short single-word OCR misses.
    """
    multi = " " in (q_raw or "").strip()
    L = len(qn or "")
    if multi:
        return PARTIAL_RATIO_BLOCK, RATIO_BLOCK
    if L <= 5:
        return 78, 76
    if L <= 7:
        return 84, 82
    return PARTIAL_RATIO_BLOCK, RATIO_BLOCK


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _norm_nospace(s: str) -> str:
    return re.sub(r"\s+", "", _norm(s))


def _is_word_char(c: str) -> bool:
    if not c:
        return False
    ch = c[0]
    cat = unicodedata.category(ch)
    return cat.startswith(("L", "N", "M"))


def _word_boundary_ok(text: str, idx: int, q_len: int) -> bool:
    """Whole keyword / phrase only — not a substring inside a longer word (Unicode-aware)."""
    if q_len < 1:
        return False
    if idx > 0 and _is_word_char(text[idx - 1]):
        return False
    end = idx + q_len
    if end < len(text) and _is_word_char(text[end]):
        return False
    return True


def _nospace_run_boundary_ok(cat: str, idx: int, qlen: int) -> bool:
    """No-space merged line: do not match in the middle of an alphanumeric run."""
    if qlen < 1 or idx < 0 or idx + qlen > len(cat):
        return False
    if idx > 0 and _is_word_char(cat[idx - 1]):
        return False
    end = idx + qlen
    if end < len(cat) and _is_word_char(cat[end]):
        return False
    return True


def _union_bbox(blocks: list[dict[str, Any]]) -> list[float]:
    xs0 = [float(b["bbox"][0]) for b in blocks]
    ys0 = [float(b["bbox"][1]) for b in blocks]
    xs1 = [float(b["bbox"][2]) for b in blocks]
    ys1 = [float(b["bbox"][3]) for b in blocks]
    return [min(xs0), min(ys0), max(xs1), max(ys1)]


def _ytight_for_char_span(y0: float, y1: float, c0: int, c1_excl: int, n: int) -> tuple[float, float]:
    """
    One text block often covers a full wrapped paragraph; horizontal slicing alone leaves a tall highlight bar.
    Approximate which visual line the substring sits on and narrow Y to that band.
    """
    y0, y1 = float(y0), float(y1)
    h = max(0.0, y1 - y0)
    if n < 2 or h < 26.0:
        return y0, y1
    est_line = max(12.0, min(24.0, h / max(1.0, round(h / 16.0))))
    n_lines = max(1, min(64, int(round(h / est_line))))
    if n_lines <= 1:
        return y0, y1
    c0 = max(0, min(c0, n - 1))
    c1_excl = max(c0, min(c1_excl, n))
    c_mid = (c0 + c1_excl) / 2.0
    cpl = n / n_lines
    line_idx = int(c_mid / cpl) if cpl > 0 else 0
    line_idx = max(0, min(line_idx, n_lines - 1))
    seg_h = h / n_lines
    y_top = y0 + line_idx * seg_h
    return y_top, y_top + seg_h


def _narrow_line_bbox_to_query(
    line_blocks: list[dict[str, Any]],
    char_start: int,
    char_len: int,
    denom_len: int,
) -> list[float]:
    """Last-resort horizontal slice when per-block mapping fails (non-linear layout)."""
    ub = _union_bbox(line_blocks)
    n = max(int(denom_len), 1)
    cs = max(0, min(int(char_start), n))
    cl = max(0, int(char_len))
    x0, y0, x1, y1 = (float(v) for v in ub)
    w = max(0.0, x1 - x0)
    t0 = cs / n
    t1 = (cs + cl) / n
    if t1 < t0:
        t0, t1 = t1, t0
    yn0, yn1 = _ytight_for_char_span(y0, y1, cs, cs + cl, n)
    return [x0 + w * t0, yn0, x0 + w * t1, yn1]


def _union_bbox_coords(boxes: list[list[float]]) -> list[float]:
    if not boxes:
        return [0.0, 0.0, 0.0, 0.0]
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[2] for b in boxes)
    y1 = max(b[3] for b in boxes)
    return [x0, y0, x1, y1]


def _slice_block_bbox_by_norm_span(b: dict[str, Any], s_norm: str, i0: int, i1: int) -> list[float]:
    """Sub-rectangle of one OCR block for characters [i0:i1) in its normalized text."""
    n = max(len(s_norm), 1)
    i0 = max(0, min(i0, n))
    i1 = max(i0, min(i1, n))
    x0, y0, x1, y1 = (float(v) for v in b["bbox"])
    w = max(0.0, x1 - x0)
    t0, t1 = i0 / n, i1 / n
    yn0, yn1 = _ytight_for_char_span(y0, y1, i0, i1, n)
    return [x0 + w * t0, yn0, x0 + w * t1, yn1]


def _bbox_for_norm_merged_substring(
    line_blocks: list[dict[str, Any]],
    merged: str,
    idx: int,
    qlen: int,
) -> list[float]:
    """
    Map character indices in ``merged`` to real OCR block geometry (tables / multi-cell rows).
    Avoids treating the whole row union as one string axis (which misplaced highlights).
    """
    meta: list[tuple[dict[str, Any], str]] = []
    for b in line_blocks:
        t = (b.get("text") or "").strip()
        if not t:
            continue
        meta.append((b, _norm(t)))
    parts: list[str] = []
    for i, (_, s) in enumerate(meta):
        if i:
            parts.append(" ")
        parts.append(s)
    recon = "".join(parts)
    if recon != merged:
        return _narrow_line_bbox_to_query(line_blocks, idx, qlen, len(merged))
    abs_start, abs_end = idx, idx + qlen
    boxes: list[list[float]] = []
    pos = 0
    for b, s in meta:
        if pos > 0:
            pos += 1
        seg_start = pos
        seg_end = pos + len(s)
        pos = seg_end
        ov0, ov1 = max(abs_start, seg_start), min(abs_end, seg_end)
        if ov1 <= ov0:
            continue
        i0 = ov0 - seg_start
        i1 = ov1 - seg_start
        boxes.append(_slice_block_bbox_by_norm_span(b, s, i0, i1))
    if not boxes:
        return _narrow_line_bbox_to_query(line_blocks, idx, qlen, len(merged))
    return _union_bbox_coords(boxes)


# Min length of digit-only key for formatted-number matching (e.g. short IDs, amounts)
MIN_DIGIT_QUERY_LEN = 2


def _digits_only(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def _query_is_digit_only(q_raw: str) -> bool:
    """Numbers / phone-style queries (no letters). Keep line_nospace & line_digits matches for these."""
    s = (q_raw or "").strip()
    if len(_digits_only(s)) < MIN_DIGIT_QUERY_LEN:
        return False
    if re.search(r"[A-Za-z]", s):
        return False
    return True


def _digit_char_spans(text: str) -> tuple[str, list[tuple[int, int]]]:
    dig: list[str] = []
    spans: list[tuple[int, int]] = []
    for i, c in enumerate(text):
        if c.isdigit():
            dig.append(c)
            spans.append((i, i + 1))
    return "".join(dig), spans


def _digit_span_boundary_ok(text: str, orig_start: int, orig_end_excl: int) -> bool:
    if orig_start > 0 and text[orig_start - 1].isdigit():
        return False
    if orig_end_excl < len(text) and text[orig_end_excl].isdigit():
        return False
    return True


def _slice_block_bbox_by_char_span(b: dict[str, Any], text: str, c0: int, c1_excl: int) -> list[float]:
    """Slice of block bbox for character range [c0, c1_excl) in ``text`` (tight X + estimated line Y)."""
    n = max(len(text), 1)
    c0 = max(0, min(int(c0), n))
    c1_excl = max(c0, min(int(c1_excl), n))
    x0, y0, x1, y1 = (float(v) for v in b["bbox"])
    w = max(0.0, x1 - x0)
    t0, t1 = c0 / n, c1_excl / n
    yn0, yn1 = _ytight_for_char_span(y0, y1, c0, c1_excl, n)
    return [x0 + w * t0, yn0, x0 + w * t1, yn1]


def _try_block_digit_formatted_match(b: dict[str, Any], qd: str) -> dict[str, Any] | None:
    """Match query digits in block text that may use spaces, dashes, parens, etc."""
    if len(qd) < MIN_DIGIT_QUERY_LEN:
        return None
    text = (b.get("text") or "").strip()
    if not text:
        return None
    ds, spans = _digit_char_spans(text)
    if not ds:
        return None
    j = 0
    while j <= len(ds) - len(qd):
        idx = ds.find(qd, j)
        if idx < 0:
            break
        orig_start = spans[idx][0]
        orig_end_excl = spans[idx + len(qd) - 1][1]
        if _digit_span_boundary_ok(text, orig_start, orig_end_excl):
            n = max(len(text), 1)
            x0, y0, x1, y1 = (float(x) for x in b["bbox"])
            w = max(0.0, x1 - x0)
            t0, t1 = orig_start / n, orig_end_excl / n
            yn0, yn1 = _ytight_for_char_span(y0, y1, orig_start, orig_end_excl, n)
            bbox = [x0 + w * t0, yn0, x0 + w * t1, yn1]
            snippet = text[max(0, orig_start - 10) : orig_end_excl + 10]
            return {
                "page": int(b.get("page", 1)),
                "bbox": bbox,
                "snippet": snippet,
            }
        j = idx + 1
    return None


def _bbox_for_digit_only_concat_substring(line_blocks: list[dict[str, Any]], qd: str) -> list[float] | None:
    """
    Same-line OCR blocks may split formatted numbers (e.g. ``82`` | ``725``).
    Concatenate per-block digit-only strings and map match back to bboxes.
    """
    if len(qd) < MIN_DIGIT_QUERY_LEN:
        return None
    pieces: list[tuple[dict[str, Any], str, str]] = []
    for b in line_blocks:
        t = (b.get("text") or "").strip()
        if not t:
            continue
        d = _digits_only(t)
        if not d:
            continue
        pieces.append((b, d, t))
    if not pieces:
        return None
    cat = "".join(p[1] for p in pieces)
    idx = cat.find(qd)
    if idx < 0:
        return None
    end = idx + len(qd)
    cur = 0
    boxes: list[list[float]] = []
    first_os0: int | None = None
    first_text: str | None = None
    last_os1_excl: int | None = None
    last_text: str | None = None
    for b, d, full_t in pieces:
        L = len(d)
        next_cur = cur + L
        ov0, ov1 = max(cur, idx), min(next_cur, end)
        if ov1 > ov0:
            di0, di1 = ov0 - cur, ov1 - cur
            _, sp = _digit_char_spans(full_t)
            if di0 < 0 or di1 > len(sp) or di0 >= di1:
                return None
            os0 = sp[di0][0]
            os1_excl = sp[di1 - 1][1]
            if first_os0 is None:
                first_os0 = os0
                first_text = full_t
            last_os1_excl = os1_excl
            last_text = full_t
            boxes.append(_slice_block_bbox_by_char_span(b, full_t, os0, os1_excl))
        cur = next_cur
    if not boxes or first_os0 is None or first_text is None or last_os1_excl is None or last_text is None:
        return None
    if first_os0 > 0 and first_text[first_os0 - 1].isdigit():
        return None
    if last_os1_excl < len(last_text) and last_text[last_os1_excl].isdigit():
        return None
    return _union_bbox_coords(boxes)


def _bbox_for_nospace_substring(line_blocks: list[dict[str, Any]], q_ns: str) -> list[float] | None:
    """Bbox for query in concatenated no-space block texts (preserves per-cell geometry)."""
    pieces: list[tuple[dict[str, Any], str]] = []
    for b in line_blocks:
        t = (b.get("text") or "").strip()
        if not t:
            continue
        pieces.append((b, _norm_nospace(t)))
    cat = "".join(p[1] for p in pieces)
    idx = cat.find(q_ns)
    if idx < 0:
        return None
    end = idx + len(q_ns)
    cur = 0
    boxes: list[list[float]] = []
    for b, p in pieces:
        L = len(p)
        next_cur = cur + L
        ov0, ov1 = max(cur, idx), min(next_cur, end)
        if ov1 > ov0:
            i0, i1 = ov0 - cur, ov1 - cur
            boxes.append(_slice_block_bbox_by_norm_span(b, p, i0, i1))
        cur = next_cur
    if not boxes:
        return None
    return _union_bbox_coords(boxes)


def _line_y_tol() -> float:
    try:
        return float(os.environ.get("SMART_FIND_OCR_LINE_Y_TOL", "24"))
    except ValueError:
        return 24.0


def group_ocr_blocks_into_lines(
    blocks: list[dict[str, Any]],
    *,
    y_tol: float | None = None,
    max_x_gap: float = 180.0,
) -> list[list[dict[str, Any]]]:
    tol = y_tol if y_tol is not None else _line_y_tol()
    by_page: dict[int, list[dict[str, Any]]] = {}
    for b in blocks:
        p = int(b.get("page", 1))
        by_page.setdefault(p, []).append(b)
    all_lines: list[list[dict[str, Any]]] = []
    for page in sorted(by_page.keys()):
        items = sorted(
            by_page[page],
            key=lambda b: (float(b["bbox"][1]), float(b["bbox"][0])),
        )
        line: list[dict[str, Any]] = []
        for b in items:
            bb = b["bbox"]
            cy = (float(bb[1]) + float(bb[3])) / 2
            if not line:
                line = [b]
                continue
            prev = line[-1]
            pbb = prev["bbox"]
            pcy = (float(pbb[1]) + float(pbb[3])) / 2
            x_gap = float(bb[0]) - float(pbb[2])
            if abs(cy - pcy) <= tol and x_gap <= max_x_gap:
                line.append(b)
            else:
                all_lines.append(line)
                line = [b]
        if line:
            all_lines.append(line)
    return all_lines


def _best_fuzzy_block_in_line(
    line_blocks: list[dict[str, Any]],
    q_raw: str,
    qn: str,
) -> tuple[list[float], str] | None:
    """
    Pick the tightest fuzzy target inside a line.
    Avoid whole-line highlight boxes for fuzzy-only hits.
    """
    pr_need, r_need = _block_fuzzy_thresholds(q_raw, qn)
    best_score = -1.0
    best_bbox: list[float] | None = None
    best_text = ""
    for b in line_blocks:
        text = (b.get("text") or "").strip()
        if len(text) < 2:
            continue
        tn = _norm(text)
        if not tn:
            continue
        pr = float(fuzz.partial_ratio(qn, tn))
        r = float(fuzz.ratio(qn, tn))
        if pr < pr_need and r < r_need:
            continue
        score = max(pr, r)
        if score > best_score:
            best_score = score
            best_bbox = [float(x) for x in b["bbox"]]
            best_text = text[:120]
    if best_bbox is None:
        return None
    return best_bbox, best_text


def _sort_blocks_for_search(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stable reading order so every search stage sees blocks in the same sequence."""

    def key(b: dict[str, Any]) -> tuple:
        bb = b.get("bbox") or [0.0, 0.0, 0.0, 0.0]
        t = (b.get("text") or "")[:96]
        return (int(b.get("page", 1)), float(bb[1]), float(bb[0]), t)

    return sorted(blocks, key=key)


def _bbox_iou(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) < 4 or len(b) < 4:
        return 0.0
    ax0, ay0, ax1, ay1 = (float(a[i]) for i in range(4))
    bx0, by0, bx1, by1 = (float(b[i]) for i in range(4))
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    iw, ih = max(0.0, ix1 - ix0), max(0.0, iy1 - iy0)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    aa = max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0)
    ba = max(0.0, bx1 - bx0) * max(0.0, by1 - by0)
    union = aa + ba - inter
    return inter / union if union > 1e-9 else 0.0


def _bbox_center_dist(a: list[float], b: list[float]) -> float:
    if len(a) < 4 or len(b) < 4:
        return 1e9
    acx = (float(a[0]) + float(a[2])) / 2
    acy = (float(a[1]) + float(a[3])) / 2
    bcx = (float(b[0]) + float(b[2])) / 2
    bcy = (float(b[1]) + float(b[3])) / 2
    return float(math.hypot(acx - bcx, acy - bcy))


def _same_visual_hit_bbox(a: list[float], b: list[float]) -> bool:
    """
    True when two OCR boxes are the same ink span (duplicate paths / jitter).
    Stricter than before so handwritten lines with multiple words are not collapsed together.
    """
    if len(a) < 4 or len(b) < 4:
        return False
    if _bbox_iou(a, b) >= 0.38:
        return True
    d = _bbox_center_dist(a, b)
    if d > 32:
        return False
    ax0, ax1 = min(float(a[0]), float(a[2])), max(float(a[0]), float(a[2]))
    bx0, bx1 = min(float(b[0]), float(b[2])), max(float(b[0]), float(b[2]))
    ay0, ay1 = min(float(a[1]), float(a[3])), max(float(a[1]), float(a[3]))
    by0, by1 = min(float(b[1]), float(b[3])), max(float(b[1]), float(b[3]))
    ix = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    iy = max(0.0, min(ay1, by1) - max(ay0, by0))
    aw, ah = max(1e-6, ax1 - ax0), max(1e-6, ay1 - ay0)
    bw, bh = max(1e-6, bx1 - bx0), max(1e-6, by1 - by0)
    oxa = ix / min(aw, bw)
    oya = iy / min(ah, bh)
    if oxa > 0.4 and oya > 0.32:
        return True
    if d < 14 and (oxa > 0.15 or oya > 0.15):
        return True
    return False


_MATCH_TYPE_RANK: dict[str, int] = {
    "exact": 0,
    "exact_loose": 1,
    "digit_formatted": 2,
    "line_merged_word": 3,
    "line": 4,
    "line_nospace": 5,
    "line_digits": 6,
    "line_substring": 7,
    "fuzzy_line": 8,
    "fuzzy_line_tokens": 9,
    "fuzzy_block": 10,
}


def _match_rank(mt: str) -> int:
    return _MATCH_TYPE_RANK.get(mt, 50)


def _finalize_ocr_matches(matches: list[dict[str, Any]], _q_raw: str) -> list[dict[str, Any]]:
    """
    Dedupe overlapping hits on the same page (OCR jitter + multiple match_types for one span).
    Keeps the highest-precision match_type. Output order: reading order.
    """
    if not matches:
        return []

    def sort_key(m: dict[str, Any]) -> tuple:
        bb = m.get("bbox") or [0.0, 0.0, 0.0, 0.0]
        return (
            int(m.get("page", 1)),
            float(bb[1]),
            float(bb[0]),
            _match_rank(str(m.get("match_type") or "")),
        )

    ordered = sorted(matches, key=sort_key)
    kept: list[dict[str, Any]] = []
    for m in ordered:
        pg = int(m.get("page", 1))
        bb = m.get("bbox") or [0.0, 0.0, 0.0, 0.0]
        mr = _match_rank(str(m.get("match_type") or ""))
        dup_idx = -1
        for i, e in enumerate(kept):
            if int(e.get("page", 1)) != pg:
                continue
            eb = e.get("bbox") or [0.0, 0.0, 0.0, 0.0]
            if _bbox_iou(bb, eb) >= 0.45 or _same_visual_hit_bbox(bb, eb):
                dup_idx = i
                break
        if dup_idx >= 0:
            er = _match_rank(str(kept[dup_idx].get("match_type") or ""))
            if mr < er:
                kept[dup_idx] = m
            continue
        kept.append(m)
    kept.sort(
        key=lambda m: (
            int(m.get("page", 1)),
            float((m.get("bbox") or [0.0])[1]),
            float((m.get("bbox") or [0.0])[0]),
        ),
    )
    return kept


def search_ocr_blocks(blocks: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    """
    Return matches: {page, bbox, snippet, match_type: exact|line|fuzzy_line|fuzzy_block}.
    """
    q_raw = (query or "").strip()
    if not q_raw or not blocks:
        return []

    qn = _norm(q_raw)
    q_ns = _norm_nospace(q_raw)
    if len(q_ns) < 1:
        return []
    single_word_query = " " not in q_raw.strip()
    blocks = _sort_blocks_for_search(blocks)

    seen: set[tuple[int, int, int, int, int]] = set()
    out: list[dict[str, Any]] = []

    def add_match(
        page: int,
        bbox: list[float],
        snippet: str,
        match_type: str,
    ) -> None:
        key = (
            page,
            int(bbox[0] // 8),
            int(bbox[1] // 8),
            int(bbox[2] // 8),
            int(bbox[3] // 8),
        )
        if key in seen:
            return
        seen.add(key)
        out.append(
            {
                "page": page,
                "bbox": bbox,
                "snippet": snippet[:200],
                "source": "ocr",
                "match_type": match_type,
            }
        )

    # 1) Exact substring per block (original behavior)
    for b in blocks:
        text = (b.get("text") or "").strip()
        if not text:
            continue
        low = text.lower()
        qlow = q_raw.lower()
        idx = low.find(qlow)
        if idx >= 0 and _word_boundary_ok(text, idx, len(q_raw)):
            n = max(len(text), 1)
            x0, y0, x1, y1 = (float(x) for x in b["bbox"])
            w = max(0.0, x1 - x0)
            t0, t1 = idx / n, (idx + len(q_raw)) / n
            yn0, yn1 = _ytight_for_char_span(y0, y1, idx, idx + len(q_raw), n)
            bbox = [x0 + w * t0, yn0, x0 + w * t1, yn1]
            if _skip_watermark_like_hit(text, bbox, q_raw):
                continue
            add_match(
                int(b.get("page", 1)),
                bbox,
                text[max(0, idx - 10) : idx + len(q_raw) + 10],
                "exact",
            )

    if out:
        return _finalize_ocr_matches(out, q_raw)

    # 1b) Substring without strict word boundaries (OCR glues tokens: "PolicyNo", "SUB-12345x")
    ql = q_raw.strip()
    if len(ql) >= 3:
        qll = ql.lower()
        for b in blocks:
            text = (b.get("text") or "").strip()
            if not text:
                continue
            low = text.lower()
            idx = low.find(qll)
            if idx < 0:
                continue
            if _word_boundary_ok(text, idx, len(ql)):
                continue
            n = max(len(text), 1)
            x0, y0, x1, y1 = (float(x) for x in b["bbox"])
            w = max(0.0, x1 - x0)
            t0, t1 = idx / n, (idx + len(ql)) / n
            yn0, yn1 = _ytight_for_char_span(y0, y1, idx, idx + len(ql), n)
            bbox = [x0 + w * t0, yn0, x0 + w * t1, yn1]
            if _skip_watermark_like_hit(text, bbox, q_raw):
                continue
            add_match(
                int(b.get("page", 1)),
                bbox,
                text[max(0, idx - 10) : idx + len(ql) + 10],
                "exact_loose",
            )

    if out:
        return _finalize_ocr_matches(out, q_raw)

    # 1a) Formatted digits in one block: "82 725", "(827) 25", "82-725" vs query "82725" or "82 725"
    qd = _digits_only(q_raw)
    if len(qd) >= MIN_DIGIT_QUERY_LEN:
        for b in blocks:
            hit = _try_block_digit_formatted_match(b, qd)
            if hit:
                add_match(
                    hit["page"],
                    hit["bbox"],
                    hit["snippet"],
                    "digit_formatted",
                )
    if out:
        return _finalize_ocr_matches(out, q_raw)

    # 2) Merged lines: boundary-safe substring on normalized / no-space
    qd_merged = _digits_only(q_raw)
    for line_blocks in group_ocr_blocks_into_lines(blocks):
        texts = [(b.get("text") or "").strip() for b in line_blocks]
        raw_line_joined = " ".join(texts).strip()
        merged = _norm(" ".join(t for t in texts if t))
        merged_ns = _norm_nospace(" ".join(texts))
        page = int(line_blocks[0].get("page", 1))
        line_hit = False
        # Single-word queries: merged line (e.g. "policy" across adjacent OCR tokens "pol"+"icy" was broken;
        # or "the" + "policy" → merged contains "policy" as a word).
        if not line_hit and single_word_query and len(qn) >= 2:
            pos_sw = 0
            while True:
                idx_sw = merged.find(qn, pos_sw)
                if idx_sw < 0:
                    break
                if _word_boundary_ok(merged, idx_sw, len(qn)):
                    bb_sw = _bbox_for_norm_merged_substring(
                        line_blocks, merged, idx_sw, len(qn)
                    )
                    if _skip_watermark_like_hit(raw_line_joined, bb_sw, q_raw):
                        pos_sw = idx_sw + 1
                        continue
                    add_match(page, bb_sw, merged[:120], "line_merged_word")
                    line_hit = True
                    break
                pos_sw = idx_sw + 1
        if len(qn) >= 3 and not single_word_query:
            pos = 0
            while True:
                idx = merged.find(qn, pos)
                if idx < 0:
                    break
                if _word_boundary_ok(merged, idx, len(qn)):
                    bb = _bbox_for_norm_merged_substring(line_blocks, merged, idx, len(qn))
                    if not _skip_watermark_like_hit(raw_line_joined, bb, q_raw):
                        add_match(page, bb, merged[:120], "line")
                        line_hit = True
                        break
                pos = idx + 1
        elif qn and qn in merged and not single_word_query:
            idx = merged.find(qn)
            if idx >= 0 and _word_boundary_ok(merged, idx, len(qn)):
                bb = _bbox_for_norm_merged_substring(line_blocks, merged, idx, len(qn))
                if not _skip_watermark_like_hit(raw_line_joined, bb, q_raw):
                    add_match(page, bb, merged[:120], "line")
                    line_hit = True

        if not line_hit and len(q_ns) >= 3 and q_ns in merged_ns:
            idx_ns = merged_ns.find(q_ns)
            if idx_ns >= 0 and _nospace_run_boundary_ok(merged_ns, idx_ns, len(q_ns)):
                bb = _bbox_for_nospace_substring(line_blocks, q_ns)
                if bb is None:
                    bb = _narrow_line_bbox_to_query(line_blocks, idx_ns, len(q_ns), len(merged_ns))
                if not _skip_watermark_like_hit(raw_line_joined, bb, q_raw):
                    add_match(page, bb, merged[:120], "line_nospace")
                    line_hit = True

        if not line_hit and len(qd_merged) >= MIN_DIGIT_QUERY_LEN:
            bb_d = _bbox_for_digit_only_concat_substring(line_blocks, qd_merged)
            if bb_d is not None:
                add_match(page, bb_d, merged[:120], "line_digits")
                line_hit = True

        if line_hit:
            continue

        if len(qn) >= FUZZY_MIN_QUERY_LEN and merged and not single_word_query:
            pr = fuzz.partial_ratio(qn, merged)
            ts = fuzz.token_sort_ratio(qn, merged)
            pr_need = _line_partial_threshold(q_raw, qn)
            fuzzy_ok = pr >= pr_need or (ts >= TOKEN_SORT_LINE and (" " not in q_raw.strip() or q_ns in merged_ns))
            if fuzzy_ok and " " in q_raw.strip() and len(q_ns) >= 6 and q_ns not in merged_ns:
                fuzzy_ok = False
            if fuzzy_ok:
                if qn not in merged and " " in q_raw.strip():
                    continue
                # Single-word fuzzy should prefer tight block-level matches, not whole-line heuristics.
                # Let stage (3) handle those with smaller bboxes.
                if qn not in merged and " " not in q_raw.strip():
                    continue
                if qn in merged:
                    idx_f = -1
                    pos_fm = 0
                    while True:
                        j = merged.find(qn, pos_fm)
                        if j < 0:
                            break
                        if _word_boundary_ok(merged, j, len(qn)):
                            idx_f = j
                            break
                        pos_fm = j + 1
                    if idx_f < 0:
                        continue
                    bb = _bbox_for_norm_merged_substring(line_blocks, merged, idx_f, len(qn))
                    snip = merged[:120]
                else:
                    tight = _best_fuzzy_block_in_line(line_blocks, q_raw, qn)
                    if tight is None:
                        continue
                    bb, snip = tight
                if _skip_watermark_like_hit(raw_line_joined, bb, q_raw):
                    continue
                add_match(
                    page,
                    bb,
                    snip,
                    "fuzzy_line" if pr >= pr_need else "fuzzy_line_tokens",
                )

    if out:
        return _finalize_ocr_matches(out, q_raw)

    # 3) Fuzzy per word token (typo-tolerant whole keyword, not a short substring inside a longer word).
    if len(qn) < 3:
        return _finalize_ocr_matches(out, q_raw)

    _pr_need_block, r_need_block = _block_fuzzy_thresholds(q_raw, qn)
    for b in blocks:
        text = (b.get("text") or "").strip()
        if len(text) < 2:
            continue
        if single_word_query:
            hit = False
            i = 0
            n = len(text)
            while i < n:
                while i < n and not _is_word_char(text[i]):
                    i += 1
                j = i
                while j < n and _is_word_char(text[j]):
                    j += 1
                if j > i:
                    tok = text[i:j]
                    tn = _norm(tok)
                    if len(tn) >= 2 and abs(len(tn) - len(qn)) <= max(2, len(qn) // 2 + 1):
                        r = fuzz.ratio(qn, tn)
                        if r >= r_need_block:
                            bb = _slice_block_bbox_by_char_span(b, text, i, j)
                            if _skip_watermark_like_hit(tok, bb, q_raw):
                                i = j
                                continue
                            add_match(
                                int(b.get("page", 1)),
                                bb,
                                text[max(0, i - 8) : j + 8],
                                "fuzzy_block",
                            )
                            hit = True
                            break
                i = j
            if hit:
                continue
            continue

        tn = _norm(text)
        if " " in q_raw.strip() and len(q_ns) >= 6 and q_ns not in _norm_nospace(tn):
            continue
        ts = fuzz.token_set_ratio(qn, tn)
        if ts >= max(r_need_block - 2, 78):
            bb_fb = [float(x) for x in b["bbox"]]
            if not _skip_watermark_like_hit(text, bb_fb, q_raw):
                add_match(
                    int(b.get("page", 1)),
                    bb_fb,
                    text[:120],
                    "fuzzy_block",
                )

    # Single-word text queries: drop broad line-level hits so highlights stay on one token.
    # Digit-only queries (e.g. handwritten phone "82725") often match only via line_nospace /
    # line_digits after OCR splits — do not strip those.
    if single_word_query:
        digit_only_q = _query_is_digit_only(q_raw)
        drop = {"line", "line_substring", "fuzzy_line", "fuzzy_line_tokens"}
        if not digit_only_q:
            drop |= {"line_nospace", "line_digits"}
        out = [m for m in out if str(m.get("match_type") or "") not in drop]

    return _finalize_ocr_matches(out, q_raw)


def search_ocr_blocks_batch(
    documents: list[dict[str, Any]],
    query: str,
) -> list[dict[str, Any]]:
    """
    Run :func:`search_ocr_blocks` on each document. Each match includes
    ``document_id`` and ``filename`` for UI batch navigation.

    Each item in **documents** must have keys ``document_id``, ``filename``, ``blocks``.
    """
    out: list[dict[str, Any]] = []
    for doc in documents:
        did = str(doc.get("document_id") or "")
        fn = str(doc.get("filename") or "")
        blocks = doc.get("blocks") or []
        if not isinstance(blocks, list):
            continue
        for m in search_ocr_blocks(blocks, query):
            row = dict(m)
            row["document_id"] = did
            row["filename"] = fn
            out.append(row)
    out.sort(
        key=lambda r: (
            str(r.get("document_id") or ""),
            int(r.get("page", 1)),
            float((r.get("bbox") or [0.0])[1]),
            float((r.get("bbox") or [0.0])[0]),
        ),
    )
    return out
