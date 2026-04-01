"""
Batch-verify extracted fields against user-defined concepts (e.g. DOB, First name).
Uses fuzzy matching on label / name / field_id so wording can differ per PDF.
"""
from __future__ import annotations

import json
import re
from typing import Any

from rapidfuzz import fuzz


def _missing_value(v: Any) -> bool:
    if v is None:
        return True
    s = str(v).strip()
    if not s:
        return True
    core = re.sub(r"[\s_\-\.]", "", s)
    return len(core) < 1


def _field_match_strings(f: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for k in ("label", "name", "field_id"):
        t = f.get(k)
        if t is not None and str(t).strip():
            out.append(str(t).strip())
    return out


def best_field_for_concept(
    concept: str,
    fields: list[dict[str, Any]],
    *,
    min_score: float = 62.0,
) -> tuple[dict[str, Any] | None, float]:
    concept_norm = concept.strip().lower()
    if not concept_norm:
        return None, 0.0
    best_f: dict[str, Any] | None = None
    best_score = 0.0
    for f in fields:
        texts = _field_match_strings(f)
        if not texts:
            continue
        score = max(fuzz.token_set_ratio(concept_norm, t.lower()) for t in texts)
        if score > best_score:
            best_score = float(score)
            best_f = f
    if best_f is None or best_score < min_score:
        return None, best_score
    return best_f, best_score


def verify_pdf_against_concepts(
    pdf_name: str,
    fields: list[dict[str, Any]],
    concepts: list[str],
    *,
    min_match_score: float = 62.0,
) -> dict[str, Any]:
    checks_out: list[dict[str, Any]] = []
    messages: list[str] = []
    for c in concepts:
        raw = (c or "").strip()
        if not raw:
            continue
        match, score = best_field_for_concept(raw, fields, min_score=min_match_score)
        if match is None:
            checks_out.append(
                {
                    "concept": raw,
                    "status": "not_found",
                    "matched_field_id": None,
                    "matched_label": None,
                    "value": None,
                    "match_score": round(score, 1),
                }
            )
            messages.append(
                f"{pdf_name}: no field matched for '{raw}' (fuzzy score {score:.0f} < {min_match_score})"
            )
            continue
        val = match.get("value")
        mid = match.get("field_id") or match.get("name")
        lbl = match.get("label") or match.get("name")
        if _missing_value(val):
            checks_out.append(
                {
                    "concept": raw,
                    "status": "null_or_empty",
                    "matched_field_id": mid,
                    "matched_label": lbl,
                    "value": val,
                    "match_score": round(score, 1),
                }
            )
            messages.append(
                f"{pdf_name}: '{raw}' matched '{lbl or mid}' but value is null or empty"
            )
        else:
            checks_out.append(
                {
                    "concept": raw,
                    "status": "ok",
                    "matched_field_id": mid,
                    "matched_label": lbl,
                    "value": val,
                    "match_score": round(score, 1),
                }
            )
    all_ok = bool(checks_out) and all(x["status"] == "ok" for x in checks_out)
    return {
        "pdf_name": pdf_name,
        "all_ok": all_ok,
        "checks": checks_out,
        "messages": messages,
    }


def parse_checks_json(checks: str) -> list[str]:
    data = json.loads(checks)
    if isinstance(data, str):
        return [data.strip()] if data.strip() else []
    if not isinstance(data, list):
        raise ValueError("checks must be a JSON array of strings or a single string")
    return [str(x).strip() for x in data if str(x).strip()]


def parse_checks_flexible(checks: str) -> list[str]:
    """JSON array, or one concept per line (plain text)."""
    s = (checks or "").strip()
    if not s:
        return []
    try:
        return parse_checks_json(s)
    except (json.JSONDecodeError, ValueError):
        pass
    return [line.strip() for line in s.splitlines() if line.strip()]
