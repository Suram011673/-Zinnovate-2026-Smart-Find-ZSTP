"""
Send session PDFs via SMTP (stdlib). Configure with environment variables:

  SMTP_HOST       — required to send (e.g. smtp.office365.com)
  SMTP_PORT       — default 587 (STARTTLS) or 465 (SSL)
  SMTP_USER       — login username if auth required
  SMTP_PASSWORD   — login password or app password
  SMTP_FROM       — From address (defaults to SMTP_USER)
  SMTP_USE_SSL    — set to 1/true for implicit SSL on port 465

If SMTP_HOST is empty, sending is disabled.

Dummy / dev (no real mail server):
  SMTP_HOST=dummy   — or: dev, noop, none (case-insensitive)
  Logged only; no network. Use for local testing and pre-ops unlock when policy requires email.
"""
from __future__ import annotations

import logging
import os
import re
import smtplib
import ssl
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Sequence

logger = logging.getLogger("smart_find.smtp")

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_DUMMY_HOSTS = frozenset({"dummy", "dev", "noop", "none"})


def _smtp_host_raw() -> str:
    return (os.environ.get("SMTP_HOST") or "").strip()


def is_dummy_smtp() -> bool:
    """True when SMTP_HOST is a reserved name — send is simulated, nothing is delivered."""
    h = _smtp_host_raw().lower()
    return bool(h) and h in _DUMMY_HOSTS


def smtp_configured() -> bool:
    return bool(_smtp_host_raw())


def parse_address_list(raw: str, *, max_addrs: int = 10) -> list[str]:
    if not raw or not isinstance(raw, str):
        return []
    parts = [p.strip() for p in re.split(r"[,;\s]+", raw) if p.strip()]
    out: list[str] = []
    for p in parts[:max_addrs]:
        if _EMAIL_RE.match(p):
            out.append(p)
    return out


def send_session_pdfs_email(
    *,
    to_addrs: Sequence[str],
    cc_addrs: Sequence[str],
    subject: str,
    body_text: str,
    attachments: Sequence[tuple[str, bytes]],
    body_html: str | None = None,
) -> None:
    host = _smtp_host_raw()
    if not host:
        raise RuntimeError("SMTP is not configured (set SMTP_HOST on the API server).")

    if host.lower() in _DUMMY_HOSTS:
        names = [fn for fn, _ in attachments]
        logger.info(
            "SMTP dummy host (%r): skipping send — to=%s subject=%r attachments=%s (%d bytes total)",
            host,
            list(to_addrs),
            subject[:120],
            names,
            sum(len(b) for _, b in attachments),
        )
        return

    port = int(os.environ.get("SMTP_PORT") or "587")
    user = (os.environ.get("SMTP_USER") or "").strip()
    password = (os.environ.get("SMTP_PASSWORD") or "").strip()
    from_addr = (os.environ.get("SMTP_FROM") or user or to_addrs[0]).strip()
    use_ssl = (os.environ.get("SMTP_USE_SSL") or "").lower() in ("1", "true", "yes")

    if not to_addrs:
        raise ValueError("At least one recipient email is required.")

    msg = MIMEMultipart()
    msg["Subject"] = subject[:998]
    msg["From"] = from_addr
    msg["To"] = ", ".join(to_addrs)
    if cc_addrs:
        msg["Cc"] = ", ".join(cc_addrs)

    if body_html and body_html.strip():
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(body_text, "plain", "utf-8"))
        alt.attach(MIMEText(body_html, "html", "utf-8"))
        msg.attach(alt)
    else:
        msg.attach(MIMEText(body_text, "plain", "utf-8"))

    for filename, data in attachments:
        safe_name = os.path.basename(filename) or "document.pdf"
        if not safe_name.lower().endswith(".pdf"):
            safe_name = f"{safe_name}.pdf"
        part = MIMEApplication(data, Name=safe_name)
        part.add_header("Content-Disposition", "attachment", filename=safe_name)
        msg.attach(part)

    all_rcpt = list(to_addrs) + list(cc_addrs)

    if use_ssl:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, context=context) as server:
            if user and password:
                server.login(user, password)
            server.sendmail(from_addr, all_rcpt, msg.as_string())
    else:
        with smtplib.SMTP(host, port) as server:
            server.ehlo()
            try:
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
            except smtplib.SMTPException:
                pass
            if user and password:
                server.login(user, password)
            server.sendmail(from_addr, all_rcpt, msg.as_string())
