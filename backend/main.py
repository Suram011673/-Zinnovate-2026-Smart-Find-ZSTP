"""
Smart Find — FastAPI backend: PDF upload, field detection, dynamic navigation API.
"""
from __future__ import annotations

import html as html_module
import logging
import os
import time
import uuid
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, Field

import ai_field_extractor as afe
import pdf_processor as pp
from batch_field_verify import parse_checks_flexible, verify_pdf_against_concepts
from document_search import search_ocr_blocks_batch
from readable_text import blocks_to_readable_document, strip_duplicate_whitespace
from decision_engine import (
    attach_priorities,
    attach_sections_to_fields,
    coerce_float,
    coerce_int,
    get_next_field,
    infer_section,
    mark_needs_review,
)
import session_email as smail

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("smart_find")

app = FastAPI(title="Smart Find API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- In-memory session: one or more PDFs (single upload = one document) ---
_session_docs: dict[str, dict[str, Any]] = {}
_session_order: list[str] = []
_session_active_id: str | None = None
_session_context: dict[str, str] = {}
_session_review_acknowledged: bool = False
_session_email_sent: bool = False
# token -> {"document_id": str, "expires": float epoch}
_share_tokens: dict[str, dict[str, Any]] = {}
_navigation_log: list[str] = []


def _trim_session_str(v: str | None, max_len: int = 200) -> str:
    if not v or not isinstance(v, str):
        return ""
    return v.strip()[:max_len]


def _session_clear() -> None:
    global _session_docs, _session_order, _session_active_id, _session_context
    global _session_review_acknowledged, _session_email_sent, _share_tokens
    _session_docs = {}
    _session_order = []
    _session_active_id = None
    _session_context = {}
    _session_review_acknowledged = False
    _session_email_sent = False
    _share_tokens = {}


def _active_doc() -> dict[str, Any] | None:
    if not _session_active_id or _session_active_id not in _session_docs:
        return None
    return _session_docs[_session_active_id]


def _ingest_pdf(data: bytes, filename: str, **extract_kw: Any) -> str:
    blocks, detected = afe.extract_fields_for_pdf(data, **extract_kw)
    doc_id = str(uuid.uuid4())
    fields: list[dict[str, Any]] = []
    for d in detected:
        row = dict(d)
        row["status"] = "pending"
        row = attach_priorities([row])[0]
        fields.append(row)
    blocks_json = pp.blocks_to_json(blocks)
    _session_docs[doc_id] = {
        "filename": filename,
        "pdf_bytes": data,
        "blocks": blocks_json,
        "fields": fields,
        "block_count": len(blocks),
    }
    return doc_id


def _store_pdf_light(data: bytes, filename: str) -> str:
    """
    Store PDF bytes + PyMuPDF text only (no Tesseract/EasyOCR). Fast path before POST /extract-documents.
    """
    blocks = pp.extract_text_blocks_pymupdf(data)
    blocks_json = pp.blocks_to_json(blocks)
    doc_id = str(uuid.uuid4())
    _session_docs[doc_id] = {
        "filename": filename,
        "pdf_bytes": data,
        "blocks": blocks_json,
        "fields": [],
        "block_count": len(blocks),
        "extraction_pending": True,
    }
    return doc_id


def _apply_full_extraction(doc_id: str, **extract_kw: Any) -> None:
    """Run full OCR + field pipeline on an already stored document."""
    d = _session_docs.get(doc_id)
    if not d or not d.get("pdf_bytes"):
        raise HTTPException(status_code=404, detail=f"Unknown document: {doc_id}")
    data = bytes(d["pdf_bytes"])
    blocks, detected = afe.extract_fields_for_pdf(data, **extract_kw)
    fields: list[dict[str, Any]] = []
    for row in detected:
        r = dict(row)
        r["status"] = "pending"
        r = attach_priorities([r])[0]
        fields.append(r)
    blocks_json = pp.blocks_to_json(blocks)
    d["blocks"] = blocks_json
    d["fields"] = fields
    d["block_count"] = len(blocks)
    d["extraction_pending"] = False


def _documents_for_batch_search() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for did in _session_order:
        d = _session_docs.get(did)
        if not d:
            continue
        out.append(
            {
                "document_id": did,
                "filename": d["filename"],
                "blocks": d["blocks"],
            }
        )
    return out


class CompleteFieldBody(BaseModel):
    field_id: str = Field(..., description="Stable id e.g. policy_number")


class ActiveDocumentBody(BaseModel):
    document_id: str = Field(..., description="Session document id from GET /documents")


class ExtractDocumentsBody(BaseModel):
    document_ids: list[str] | None = Field(
        None,
        description="If omitted or empty, run full OCR/extraction on every document in the session.",
    )


class SendSessionEmailBody(BaseModel):
    to_email: str = Field(..., description="Recipient(s), comma-separated")
    cc: str = Field("", description="Optional CC, comma-separated")
    public_api_base: str = Field(
        "",
        description="Public API base URL for time-limited PDF preview links, e.g. https://api.example.com",
    )
    public_app_url: str = Field(
        "",
        description="App URL included in the email body (how to open Smart PDF Navigator)",
    )


def _session_has_binary_pdfs() -> bool:
    for did in _session_order:
        d = _session_docs.get(did)
        if d and d.get("pdf_bytes"):
            return True
    return False


def _email_required_by_policy() -> bool:
    if os.environ.get("SMART_FIND_REQUIRE_EMAIL_BEFORE_OPS", "").lower() in (
        "1",
        "true",
        "yes",
    ):
        return True
    if os.environ.get("SMART_FIND_REQUIRE_EMAIL_IF_SMTP", "").lower() in (
        "1",
        "true",
        "yes",
    ):
        return smail.smtp_configured()
    return False


def _share_ttl_seconds() -> int:
    try:
        h = float(os.environ.get("SMART_FIND_SHARE_TTL_HOURS", "168") or "168")
    except ValueError:
        h = 168.0
    return max(1, int(h * 3600))


def _purge_expired_share_tokens() -> None:
    now = time.time()
    dead = [t for t, meta in _share_tokens.items() if float(meta.get("expires") or 0) < now]
    for t in dead:
        _share_tokens.pop(t, None)


def _new_share_token_for_document(document_id: str) -> str:
    _purge_expired_share_tokens()
    tok = uuid.uuid4().hex + uuid.uuid4().hex
    _share_tokens[tok] = {
        "document_id": document_id,
        "expires": time.time() + _share_ttl_seconds(),
    }
    return tok


def _resolve_share_token(token: str) -> str | None:
    _purge_expired_share_tokens()
    meta = _share_tokens.get(token)
    if not meta:
        return None
    if float(meta.get("expires") or 0) < time.time():
        _share_tokens.pop(token, None)
        return None
    did = meta.get("document_id")
    return str(did) if did else None


def _public_api_base(body_base: str = "") -> str:
    raw = (body_base or os.environ.get("SMART_FIND_PUBLIC_API_URL") or "").strip().rstrip("/")
    return raw


def _navigation_allowed() -> tuple[bool, str]:
    """Search and field navigation are allowed whenever a session exists (no pre-review gate)."""
    return True, ""


def _require_navigation_allowed() -> None:
    ok, msg = _navigation_allowed()
    if not ok:
        raise HTTPException(status_code=403, detail=msg)


def _normalize_field(raw: dict[str, Any]) -> dict[str, Any]:
    """Ensure FieldState shape for API responses."""
    fid = raw.get("field_id") or raw.get("name")
    conf = coerce_float(raw.get("confidence"), 0.0)
    page_i = coerce_int(raw.get("page"), 1)
    bbox = raw.get("bbox", [0, 0, 0, 0])
    section = raw.get("section")
    if not section and isinstance(bbox, (list, tuple)) and len(bbox) >= 4:
        section = infer_section(page_i, list(bbox), {page_i: 792.0})
    merged = {
        "field_id": fid,
        "name": raw.get("name", fid),
        "label": raw.get("label"),
        "value": raw.get("value"),
        "detection": raw.get("detection"),
        "bbox": bbox,
        "page": page_i,
        "confidence": conf,
        "status": raw.get("status", "pending"),
        "priority": coerce_int(raw.get("priority"), 99),
        "section": section or "unknown",
        "needs_review": conf < 0.7,
        "requires_verification": conf < 0.7,
    }
    return mark_needs_review(merged)


def _log_nav(action: str, detail: str = "") -> None:
    from datetime import datetime, timezone

    ts = datetime.now(timezone.utc).isoformat()
    line = f"{ts} {action} {detail}".strip()
    _navigation_log.append(line)
    logger.info(line)


def mock_field_seed() -> list[dict[str, Any]]:
    """
    Deterministic mock for demos (multi-page, mixed confidence).
    Bboxes are in PDF points on a letter-size-like page for viewer tests.
    """
    return [
        {
            "field_id": "policy_number",
            "name": "policy_number",
            "value": "POL-998877",
            "bbox": [72, 120, 280, 148],
            "page": 1,
            "confidence": 0.91,
            "status": "pending",
            "priority": 1,
        },
        {
            "field_id": "full_name",
            "name": "full_name",
            "value": "Jane Q. Public",
            "bbox": [72, 220, 320, 252],
            "page": 1,
            "confidence": 0.62,
            "status": "pending",
            "priority": 2,
        },
        {
            "field_id": "date_of_birth",
            "name": "date_of_birth",
            "value": "04/15/1988",
            "bbox": [72, 400, 200, 428],
            "page": 2,
            "confidence": 0.88,
            "status": "pending",
            "priority": 3,
        },
        {
            "field_id": "policy_number_alt",
            "name": "policy_number_alt",
            "value": "SUB-445566",
            "bbox": [72, 180, 260, 196],
            "page": 3,
            "confidence": 0.45,
            "status": "pending",
            "priority": 4,
        },
    ]


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ocr")
def health_ocr() -> dict[str, Any]:
    """Check Tesseract path/version and EasyOCR import (use after installing OCR)."""
    return pp.ocr_engine_status()


@app.get("/readable-text")
def readable_text() -> dict[str, Any]:
    """OCR blocks for the **active** document only."""
    ad = _active_doc()
    if not ad:
        return {
            "pages": [],
            "paragraph_count": 0,
            "line_count": 0,
            "full_text": "",
            "document_id": None,
        }
    raw = blocks_to_readable_document(list(ad["blocks"]))
    raw["full_text"] = strip_duplicate_whitespace(raw.get("full_text", ""))
    raw["document_id"] = _session_active_id
    raw["filename"] = ad["filename"]
    return raw


@app.get("/document-search")
def document_search(q: str = "") -> dict[str, Any]:
    """
    Search OCR/native blocks across **all** PDFs in the current session (batch search).
    Each match includes ``document_id`` and ``filename`` when more than one doc exists.
    """
    _require_navigation_allowed()
    docs = _documents_for_batch_search()
    matches = search_ocr_blocks_batch(docs, q) if docs else []
    return {"matches": matches, "count": len(matches)}


@app.get("/document-blocks")
def document_blocks() -> dict[str, Any]:
    """OCR blocks for the **active** document."""
    ad = _active_doc()
    if not ad:
        return {"blocks": [], "count": 0, "document_id": None}
    return {
        "blocks": list(ad["blocks"]),
        "count": len(ad["blocks"]),
        "document_id": _session_active_id,
    }


@app.get("/documents")
def list_documents() -> dict[str, Any]:
    """All PDFs in the session + active id (for viewer / batch search)."""
    items: list[dict[str, Any]] = []
    for did in _session_order:
        d = _session_docs.get(did)
        if not d:
            continue
        items.append(
            {
                "document_id": did,
                "filename": d["filename"],
                "field_count": len(d.get("fields") or []),
                "block_count": int(d.get("block_count") or 0),
            }
        )
    return {
        "documents": items,
        "active_document_id": _session_active_id,
        "count": len(items),
        "session_context": dict(_session_context),
    }


@app.get("/session/workflow-status")
def session_workflow_status() -> dict[str, Any]:
    """Session flags for optional clients; navigation is not gated on review acknowledgement."""
    allowed, reason = _navigation_allowed()
    return {
        "has_uploaded_pdfs": _session_has_binary_pdfs(),
        "review_acknowledged": True,
        "email_sent": _session_email_sent,
        "email_required_by_policy": False,
        "email_must_precede_ack": False,
        "smtp_configured": smail.smtp_configured(),
        "smtp_dummy": smail.is_dummy_smtp(),
        "navigation_allowed": allowed,
        "navigation_blocked_reason": reason if not allowed else "",
        "share_ttl_hours": round(_share_ttl_seconds() / 3600, 2),
    }


@app.post("/session/acknowledge-review")
def session_acknowledge_review() -> dict[str, Any]:
    """Call after the processor confirms they reviewed PDF(s) in the viewer."""
    global _session_review_acknowledged
    if not _session_has_binary_pdfs():
        raise HTTPException(
            status_code=400,
            detail="No uploaded PDF session to acknowledge (upload PDFs first).",
        )
    _session_review_acknowledged = True
    _log_nav("ACK_REVIEW", "processor acknowledged PDF review")
    allowed, reason = _navigation_allowed()
    return {
        "ok": True,
        "navigation_allowed": allowed,
        "navigation_blocked_reason": reason if not allowed else "",
    }


@app.post("/send-session-email")
def send_session_email(body: SendSessionEmailBody) -> dict[str, Any]:
    """
    Email all PDFs in the current session as attachments, plus transaction context and optional app URL.
    Requires SMTP_* environment variables on the server.
    """
    global _session_email_sent
    if not _session_has_binary_pdfs():
        raise HTTPException(status_code=400, detail="No PDFs in session to attach.")

    to_list = smail.parse_address_list(body.to_email)
    if not to_list:
        raise HTTPException(
            status_code=400,
            detail="Provide at least one valid recipient email in to_email.",
        )
    cc_list = smail.parse_address_list(body.cc)

    ctx = dict(_session_context)
    tt = ctx.get("transaction_type") or "Session"
    subj = f"[Smart PDF Navigator] {tt} — {len(_session_order)} PDF(s)"

    names = []
    attachments: list[tuple[str, bytes]] = []
    for did in _session_order:
        d = _session_docs.get(did)
        if not d or not d.get("pdf_bytes"):
            continue
        fn = str(d.get("filename") or f"{did}.pdf")
        names.append(fn)
        attachments.append((fn, bytes(d["pdf_bytes"])))

    if not attachments:
        raise HTTPException(status_code=400, detail="No PDF bytes available to send.")

    public_url = (body.public_app_url or os.environ.get("SMART_FIND_PUBLIC_APP_URL") or "").strip()
    api_base = _public_api_base(body.public_api_base)
    share_links: list[dict[str, str]] = []
    link_lines: list[str] = []
    html_link_items: list[str] = []
    ttl_h = round(_share_ttl_seconds() / 3600, 1)
    for did in _session_order:
        d = _session_docs.get(did)
        if not d or not d.get("pdf_bytes"):
            continue
        fn = str(d.get("filename") or f"{did}.pdf")
        token = _new_share_token_for_document(did)
        if api_base:
            full = f"{api_base}/share/pdf/{token}"
            share_links.append({"filename": fn, "url": full, "token": token})
            link_lines.append(f"  • {fn}\n    {full}")
            href = html_module.escape(full, quote=True)
            html_link_items.append(
                f"<li><strong>{html_module.escape(fn)}</strong><br/>"
                f'<a href="{href}">{html_module.escape(full)}</a></li>'
            )
        else:
            share_links.append(
                {
                    "filename": fn,
                    "url": "",
                    "token": token,
                    "note": "Set public API base (UI or SMART_FIND_PUBLIC_API_URL) to include clickable links.",
                }
            )

    lines = [
        "The following PDF(s) were loaded for processing in Smart PDF Navigator.",
        "",
        f"Files ({len(attachments)}): " + "; ".join(names),
        "",
        "Transaction context:",
        f"  Transaction type: {ctx.get('transaction_type') or '—'}",
        f"  Carrier: {ctx.get('carrier') or '—'}",
        f"  Vertical / LOB: {ctx.get('vertical') or '—'}",
        "",
        "PDFs are attached. Recipients can open attachments or use the preview links below (same files, time-limited).",
    ]
    if link_lines:
        lines.extend(
            [
                "",
                f"Preview links (expire in ~{ttl_h} hours):",
                *link_lines,
            ]
        )
    elif share_links and not api_base:
        lines.extend(
            [
                "",
                "Preview links were not included: set Public API base in the app or SMART_FIND_PUBLIC_API_URL on the server.",
            ]
        )
    if public_url:
        lines.extend(
            [
                "",
                f"Open the processing application: {public_url}",
                "(Use the same environment to upload or continue the session as needed.)",
            ]
        )
    body_text = "\n".join(lines)

    body_html: str | None = None
    if link_lines and html_link_items:
        plain_head = body_text.split("Preview links", 1)[0].rstrip()
        body_html = (
            "<html><body>"
            f'<p style="white-space:pre-wrap;font-family:sans-serif">'
            f"{html_module.escape(plain_head)}</p>"
            f'<p><strong>Preview links</strong> (expire in ~{ttl_h} hours):</p><ul>'
            + "".join(html_link_items)
            + "</ul>"
        )
        if public_url:
            href = html_module.escape(public_url, quote=True)
            body_html += (
                "<p>Open the processing application: "
                f'<a href="{href}">{html_module.escape(public_url)}</a></p>'
            )
        body_html += "</body></html>"

    try:
        smail.send_session_pdfs_email(
            to_addrs=to_list,
            cc_addrs=cc_list,
            subject=subj,
            body_text=body_text,
            attachments=attachments,
            body_html=body_html,
        )
    except Exception as e:
        logger.exception("send_session_email failed")
        raise HTTPException(status_code=503, detail=str(e)) from e

    _session_email_sent = True
    _log_nav("EMAIL_SENT", f"to={to_list[0]} count={len(attachments)}")
    allowed, reason = _navigation_allowed()
    return {
        "ok": True,
        "sent_attachments": len(attachments),
        "share_links": share_links,
        "navigation_allowed": allowed,
        "navigation_blocked_reason": reason if not allowed else "",
    }


@app.get("/share/pdf/{token}")
def share_pdf(token: str) -> Response:
    """
    Time-limited inline PDF for recipients who received a link by email.
    Does not require the pre-ops navigation gate.
    """
    did = _resolve_share_token(token)
    if not did or did not in _session_docs:
        raise HTTPException(status_code=404, detail="Invalid or expired link.")
    d = _session_docs[did]
    raw = d.get("pdf_bytes")
    if not raw:
        raise HTTPException(status_code=404, detail="PDF no longer available in this session.")
    fn = str(d.get("filename") or "document.pdf")
    safe = "".join(c for c in fn if c.isalnum() or c in "._- ")[:180] or "document.pdf"
    if not safe.lower().endswith(".pdf"):
        safe = f"{safe}.pdf"
    return Response(
        content=bytes(raw),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{safe}"',
            "Cache-Control": "private, no-store",
        },
    )


@app.post("/active-document")
def set_active_document(body: ActiveDocumentBody) -> dict[str, Any]:
    """Switch viewer + field queue to another uploaded PDF."""
    global _session_active_id
    if body.document_id not in _session_docs:
        raise HTTPException(status_code=404, detail="Unknown document_id")
    _session_active_id = body.document_id
    ad = _session_docs[body.document_id]
    _log_nav("ACTIVE_DOC", f"id={body.document_id} file={ad['filename']}")
    return {
        "ok": True,
        "active_document_id": _session_active_id,
        "filename": ad["filename"],
    }


@app.get("/fields")
def list_fields() -> dict[str, Any]:
    """Fields for the **active** document."""
    ad = _active_doc()
    fields = ad["fields"] if ad else []
    name = ad["filename"] if ad else ""
    return {
        "fields": [_normalize_field(f) for f in fields],
        "pdf_name": name,
        "document_id": _session_active_id,
        "navigation_log": _navigation_log[-50:],
    }


@app.get("/next-field")
def next_field() -> dict[str, Any]:
    """
    Dynamically compute next field: pending, sorted by priority then confidence.
    """
    _require_navigation_allowed()
    ad = _active_doc()
    if not ad:
        return {"field": None, "message": "No document loaded"}
    nxt = get_next_field(ad["fields"])
    if nxt is None:
        _log_nav("NEXT_FIELD", "none (queue empty)")
        return {"field": None, "message": "No pending fields"}
    _log_nav(
        "NEXT_FIELD",
        f"field_id={nxt.get('field_id')} page={nxt.get('page')} conf={nxt.get('confidence')}",
    )
    return {"field": _normalize_field(nxt)}


@app.post("/complete-field")
def complete_field(body: CompleteFieldBody) -> dict[str, Any]:
    """Mark a field completed on the **active** document."""
    _require_navigation_allowed()
    ad = _active_doc()
    if not ad:
        raise HTTPException(status_code=400, detail="No active document")
    fid = body.field_id
    found = False
    for f in ad["fields"]:
        if f.get("field_id") == fid:
            f["status"] = "completed"
            found = True
            break
    if not found:
        raise HTTPException(status_code=404, detail=f"Unknown field_id: {fid}")
    _log_nav("COMPLETE", f"field_id={fid}")
    nxt = get_next_field(ad["fields"])
    return {
        "ok": True,
        "completed": fid,
        "next": _normalize_field(nxt) if nxt else None,
    }


@app.post("/reset")
def reset() -> dict[str, str]:
    global _navigation_log
    _session_clear()
    _navigation_log = []
    _log_nav("RESET", "all")
    return {"status": "reset"}


@app.post("/seed-mock")
def seed_mock() -> dict[str, Any]:
    """Load mock fields for UI/testing without a PDF."""
    global _session_docs, _session_order, _session_active_id
    _session_clear()
    fields = [dict(f) for f in mock_field_seed()]
    fields = attach_priorities(fields)
    for f in fields:
        p = coerce_int(f.get("page"), 1)
        bb = f.get("bbox", [0, 0, 0, 792])
        f["section"] = infer_section(p, list(bb), {p: 792.0})
    mid = "mock"
    _session_docs[mid] = {
        "filename": "(mock)",
        "blocks": [],
        "fields": fields,
        "block_count": 0,
    }
    _session_order = [mid]
    _session_active_id = mid
    _log_nav("SEED_MOCK", f"count={len(fields)}")
    return {"ok": True, "count": len(fields), "fields": [_normalize_field(f) for f in fields]}


def _extract_kw(
    *,
    ocr: bool = True,
    aggressive_ocr: bool = True,
    handwriting_merge: bool = False,
    dynamic_fields: bool = True,
    use_openai: bool = False,
    use_gpt_validate: bool = False,
    use_transformers: bool = False,
) -> dict[str, Any]:
    return {
        "use_ocr_fallback": ocr,
        "aggressive_ocr": aggressive_ocr,
        "handwriting_merge": handwriting_merge,
        "dynamic_fields": dynamic_fields,
        "use_openai": use_openai,
        "use_gpt_validate": use_gpt_validate,
        "use_transformers": use_transformers,
    }


@app.post("/extract-documents")
def extract_documents(
    body: ExtractDocumentsBody | None = None,
    ocr: bool = Query(True),
    aggressive_ocr: bool = Query(True),
    handwriting_merge: bool = Query(False),
    dynamic_fields: bool = Query(True),
    use_openai: bool = Query(False),
    use_gpt_validate: bool = Query(False),
    use_transformers: bool = Query(False),
) -> dict[str, Any]:
    """
    Run full OCR + field extraction on stored PDF(s). Use after upload with ``defer_extraction=true``.
    If ``document_ids`` is omitted or empty, processes every document in the current session.
    """
    kw = _extract_kw(
        ocr=ocr,
        aggressive_ocr=aggressive_ocr,
        handwriting_merge=handwriting_merge,
        dynamic_fields=dynamic_fields,
        use_openai=use_openai,
        use_gpt_validate=use_gpt_validate,
        use_transformers=use_transformers,
    )
    ids = list((body.document_ids if body else None) or [])
    if not ids:
        ids = list(_session_order)
    if not ids:
        raise HTTPException(status_code=400, detail="No documents in session to extract.")
    errors: list[dict[str, Any]] = []
    for did in ids:
        if did not in _session_docs:
            errors.append({"document_id": did, "detail": "not in session"})
            continue
        try:
            _apply_full_extraction(did, **kw)
        except Exception as e:
            logger.exception("extract-documents failed: %s", did)
            errors.append({"document_id": did, "detail": str(e)})

    documents_out: list[dict[str, Any]] = []
    for did in _session_order:
        if did not in _session_docs:
            continue
        d = _session_docs[did]
        documents_out.append(
            {
                "document_id": did,
                "filename": d["filename"],
                "field_count": len(d.get("fields") or []),
                "block_count": int(d.get("block_count") or 0),
            }
        )

    if not _session_active_id or _session_active_id not in _session_docs:
        raise HTTPException(status_code=500, detail="Session state inconsistent after extract.")

    first = _session_docs[_session_active_id]
    nfields = len(first["fields"])
    nblocks = first["block_count"]
    out: dict[str, Any] = {
        "ok": len(errors) == 0,
        "documents": documents_out,
        "active_document_id": _session_active_id,
        "errors": errors,
        "fields": [_normalize_field(f) for f in first["fields"]],
        "block_count": nblocks,
        "extraction_empty": nfields == 0,
        "extraction_pending": False,
    }
    if not first["fields"]:
        if nblocks == 0:
            out["hint"] = (
                "No text was extracted (0 blocks). Open GET /health/ocr in the API to verify OCR. "
                "Install Tesseract for scans."
            )
        else:
            out["hint"] = (
                f"OCR found {nblocks} text snippets but no label:value fields. "
                "Try OPENAI_API_KEY or a clearer scan."
            )
    _log_nav("EXTRACT", f"docs={ids} errors={len(errors)}")
    return out


@app.post("/upload-pdf")
async def upload_pdf(
    file: UploadFile = File(...),
    ocr: bool = True,
    aggressive_ocr: bool = True,
    handwriting_merge: bool = False,
    dynamic_fields: bool = True,
    use_openai: bool = False,
    use_gpt_validate: bool = False,
    use_transformers: bool = False,
    include_blocks: bool = False,
    defer_extraction: bool = Query(
        False,
        description="If true, only store PDF + fast PyMuPDF text; run POST /extract-documents for OCR/fields.",
    ),
) -> dict[str, Any]:
    """
    Upload one PDF; replaces the session with a single document (same as batch with one file).
    """
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Please upload a .pdf file")
    data = await file.read()
    global _session_docs, _session_order, _session_active_id
    _session_clear()
    kw = _extract_kw(
        ocr=ocr,
        aggressive_ocr=aggressive_ocr,
        handwriting_merge=handwriting_merge,
        dynamic_fields=dynamic_fields,
        use_openai=use_openai,
        use_gpt_validate=use_gpt_validate,
        use_transformers=use_transformers,
    )
    try:
        if defer_extraction:
            doc_id = _store_pdf_light(data, file.filename)
        else:
            doc_id = _ingest_pdf(data, file.filename, **kw)
    except Exception as e:
        logger.exception("PDF / AI extraction failed")
        raise HTTPException(status_code=400, detail=str(e)) from e
    _session_order = [doc_id]
    _session_active_id = doc_id
    ad = _session_docs[doc_id]
    nfields = len(ad["fields"])
    nblocks = ad["block_count"]
    _log_nav("UPLOAD", f"{file.filename} blocks={nblocks} fields={nfields}")

    out: dict[str, Any] = {
        "ok": True,
        "filename": file.filename,
        "document_id": doc_id,
        "documents": [
            {
                "document_id": doc_id,
                "filename": file.filename,
                "field_count": nfields,
                "block_count": nblocks,
            }
        ],
        "active_document_id": doc_id,
        "block_count": nblocks,
        "fields": [_normalize_field(f) for f in ad["fields"]],
        "extraction_empty": nfields == 0,
        "extraction_pending": bool(ad.get("extraction_pending")),
    }
    if not ad["fields"]:
        logger.warning("Upload produced no fields: %s blocks=%s", file.filename, nblocks)
        if ad.get("extraction_pending"):
            out["hint"] = (
                "PDF stored. Full OCR and field extraction will run next (POST /extract-documents). "
                "Image-only PDFs may show 0 text blocks until then."
            )
        elif nblocks == 0:
            out["hint"] = (
                "No text was extracted (0 blocks). Open GET /health/ocr in the API to verify OCR. "
                "Install Tesseract (https://github.com/UB-Mannheim/tesseract/wiki) or run install-tesseract-windows.ps1; "
                "run install-ocr-deps.bat for pip packages. Restart the API after install. "
                "Optional: SMART_FIND_OCR_DPI=260. Browse the PDF still; use Load mock fields only for demo navigation."
            )
        else:
            out["hint"] = (
                f"OCR found {nblocks} text snippets but no label:value fields. "
                "Try OPENAI_API_KEY for GPT structuring, or a clearer scan. "
                "Use “Load mock fields” to demo the UI."
            )
    if include_blocks:
        out["blocks"] = list(ad["blocks"])
    return out


@app.post("/upload-pdf-batch")
async def upload_pdf_batch(
    files: list[UploadFile] = File(..., description="Repeat field name 'files' for each PDF"),
    checks: str | None = Form(None),
    transaction_type: str | None = Form(None),
    carrier: str | None = Form(None),
    vertical: str | None = Form(None),
    ocr: bool = Query(True),
    aggressive_ocr: bool = Query(True),
    handwriting_merge: bool = Query(False),
    dynamic_fields: bool = Query(True),
    use_openai: bool = Query(False),
    use_gpt_validate: bool = Query(False),
    use_transformers: bool = Query(False),
    min_match_score: float = Query(62.0, ge=40.0, le=100.0),
    defer_extraction: bool = Query(
        False,
        description="If true, only store PDFs + fast PyMuPDF text; run POST /extract-documents for OCR/fields.",
    ),
) -> dict[str, Any]:
    """
    Upload one or more PDFs. Multipart: **files**; optional **checks**, **transaction_type**,
    **carrier**, **vertical** (session labels; do not change extraction). Booleans via query string.
    """
    if not files:
        raise HTTPException(status_code=400, detail="No PDFs received (empty files list).")
    if defer_extraction and checks is not None and str(checks).strip():
        raise HTTPException(
            status_code=400,
            detail="Cannot combine defer_extraction with batch checks; call POST /extract-documents first, then re-run verification if needed.",
        )

    kw = _extract_kw(
        ocr=ocr,
        aggressive_ocr=aggressive_ocr,
        handwriting_merge=handwriting_merge,
        dynamic_fields=dynamic_fields,
        use_openai=use_openai,
        use_gpt_validate=use_gpt_validate,
        use_transformers=use_transformers,
    )

    global _session_docs, _session_order, _session_active_id, _session_context
    _session_clear()
    _session_context = {
        "transaction_type": _trim_session_str(transaction_type),
        "carrier": _trim_session_str(carrier),
        "vertical": _trim_session_str(vertical),
    }
    documents_out: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for upload_index, file in enumerate(files):
        fn = file.filename or "unknown.pdf"
        if not fn.lower().endswith(".pdf"):
            errors.append({"upload_index": upload_index, "filename": fn, "detail": "skipped — not a .pdf"})
            continue
        data = await file.read()
        try:
            if defer_extraction:
                doc_id = _store_pdf_light(data, fn)
            else:
                doc_id = _ingest_pdf(data, fn, **kw)
        except Exception as e:
            logger.exception("batch upload extraction failed: %s", fn)
            errors.append({"upload_index": upload_index, "filename": fn, "detail": str(e)})
            continue
        ad = _session_docs[doc_id]
        documents_out.append(
            {
                "document_id": doc_id,
                "filename": fn,
                "upload_index": upload_index,
                "field_count": len(ad["fields"]),
                "block_count": ad["block_count"],
            }
        )
        _session_order.append(doc_id)

    if not _session_order:
        raise HTTPException(
            status_code=400,
            detail="No PDF could be processed. " + (errors[0]["detail"] if errors else ""),
        )

    _session_active_id = _session_order[0]
    _log_nav(
        "UPLOAD_BATCH",
        f"count={len(_session_order)} active={_session_docs[_session_active_id]['filename']}",
    )

    verification: dict[str, Any] | None = None
    if checks is not None and str(checks).strip():
        concepts = parse_checks_flexible(str(checks).strip())
        if concepts:
            min_sc = max(40.0, min(min_match_score, 100.0))
            v_results: list[dict[str, Any]] = []
            v_messages: list[str] = []
            for did in _session_order:
                d = _session_docs[did]
                row = verify_pdf_against_concepts(
                    d["filename"], d["fields"], concepts, min_match_score=min_sc
                )
                row["document_id"] = did
                v_results.append(row)
                v_messages.extend(row.get("messages") or [])
            pdfs_ok = sum(1 for r in v_results if r.get("all_ok"))
            verification = {
                "concepts": concepts,
                "min_match_score": min_sc,
                "results": v_results,
                "all_messages": v_messages,
                "summary": {
                    "pdf_count": len(v_results),
                    "concepts_count": len(concepts),
                    "pdfs_all_ok": pdfs_ok,
                    "pdfs_with_issues": len(v_results) - pdfs_ok,
                    "issue_lines": len(v_messages),
                },
            }

    first = _session_docs[_session_active_id]
    any_pending = any(bool(_session_docs[d].get("extraction_pending")) for d in _session_order if d in _session_docs)
    out: dict[str, Any] = {
        "ok": True,
        "documents": documents_out,
        "active_document_id": _session_active_id,
        "errors": errors,
        "fields": [_normalize_field(f) for f in first["fields"]],
        "block_count": first["block_count"],
        "extraction_empty": len(first["fields"]) == 0,
        "session_context": dict(_session_context),
        "extraction_pending": any_pending,
    }
    if not first["fields"]:
        if any_pending:
            out["hint"] = (
                "PDFs stored. Full OCR and field extraction will run next (POST /extract-documents)."
            )
        elif first["block_count"] == 0:
            out["hint"] = (
                "No text was extracted (0 blocks). Open GET /health/ocr in the API to verify OCR."
            )
        else:
            out["hint"] = (
                f"OCR found {first['block_count']} text snippets but no label:value fields. "
                "Try OPENAI_API_KEY or a clearer scan."
            )
    if verification is not None:
        out["verification"] = verification
    return out


@app.post("/batch-verify-fields")
async def batch_verify_fields(
    files: list[UploadFile] = File(..., description="Repeat field name 'files' for each PDF"),
    checks: str = Form(..., description="JSON array or one concept per line"),
    ocr: bool = Query(True),
    aggressive_ocr: bool = Query(True),
    handwriting_merge: bool = Query(False),
    dynamic_fields: bool = Query(True),
    use_openai: bool = Query(False),
    use_gpt_validate: bool = Query(False),
    use_transformers: bool = Query(False),
    min_match_score: float = Query(62.0, ge=40.0, le=100.0),
) -> dict[str, Any]:
    """
    Verify many PDFs (multipart **files** + **checks** form field). Options via query string.
    Does **not** replace the viewer session.
    """
    if not files:
        raise HTTPException(status_code=400, detail="No PDFs received (empty files list).")

    concepts = parse_checks_flexible(checks.strip())
    if not concepts:
        raise HTTPException(
            status_code=400,
            detail="Provide checks: JSON array of strings or one concept per line.",
        )

    min_sc = max(40.0, min(min_match_score, 100.0))

    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for file in files:
        fn = file.filename or "unknown.pdf"
        if not fn.lower().endswith(".pdf"):
            errors.append({"pdf_name": fn, "detail": "skipped — not a .pdf"})
            continue
        data = await file.read()
        try:
            _, detected = afe.extract_fields_for_pdf(
                data,
                use_ocr_fallback=ocr,
                aggressive_ocr=aggressive_ocr,
                handwriting_merge=handwriting_merge,
                dynamic_fields=dynamic_fields,
                use_openai=use_openai,
                use_gpt_validate=use_gpt_validate,
                use_transformers=use_transformers,
            )
        except Exception as e:
            logger.exception("batch verify extraction failed: %s", fn)
            errors.append({"pdf_name": fn, "detail": str(e)})
            continue
        row = verify_pdf_against_concepts(
            fn, detected, concepts, min_match_score=min_sc
        )
        row["field_count"] = len(detected)
        results.append(row)

    issues = sum(1 for r in results if not r.get("all_ok"))
    flat_messages: list[str] = []
    for r in results:
        flat_messages.extend(r.get("messages") or [])
    for e in errors:
        flat_messages.append(f"{e['pdf_name']}: ERROR — {e['detail']}")

    return {
        "ok": True,
        "concepts": concepts,
        "min_match_score": min_sc,
        "summary": {
            "total_uploaded": len(files),
            "processed_ok": len(results),
            "failed_read_or_extract": len(errors),
            "pdfs_with_missing_or_unmatched": issues + len(errors),
        },
        "results": results,
        "errors": errors,
        "all_messages": flat_messages,
    }


if __name__ == "__main__":
    import os
    import uvicorn

    port = int(os.environ.get("SMART_FIND_API_PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
