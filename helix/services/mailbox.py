"""Riu's agentic mailbox on Hostinger Mail (send + inbound)."""

from __future__ import annotations

import hmac
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import httpx
from sqlalchemy.orm import Session

from helix.config import get_settings
from helix.db import models as m

logger = logging.getLogger(__name__)

MAIL_API = "https://api.mail.hostinger.com"
_MAX_BODY = 180_000
_EMAIL_IN_ANGLE = re.compile(r"<([^>]+)>")
_INBOX = "INBOX"


def _uid(prefix: str = "mb_") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def parse_email_addr(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    found = _EMAIL_IN_ANGLE.search(text)
    if found:
        return found.group(1).strip().lower()
    return text.lower()


def mailbox_api_key() -> str:
    settings = get_settings()
    return (
        settings.hostinger_mail_api_token or settings.riu_mailbox_api_key or ""
    ).strip()


def mailbox_from() -> str:
    settings = get_settings()
    return (
        settings.riu_mailbox_from_email
        or settings.riu_mailbox_address
        or ""
    ).strip()


def mailbox_address_raw() -> str:
    settings = get_settings()
    return (settings.riu_mailbox_address or mailbox_from()).strip()


def mailbox_address() -> str:
    return parse_email_addr(mailbox_address_raw()) or mailbox_address_raw()


def mailbox_configured() -> bool:
    return bool(mailbox_api_key() and mailbox_from())


def can_use_riu_mailbox(user: m.User | None) -> bool:
    return bool(user and user.is_superadmin)


def mailbox_status() -> dict[str, Any]:
    settings = get_settings()
    return {
        "configured": mailbox_configured(),
        "provider": "hostinger",
        "address": mailbox_address_raw(),
        "from": mailbox_from(),
        "key_configured": bool(mailbox_api_key()),
        "mailbox_id_configured": bool(settings.hostinger_mail_mailbox_id),
        "webhook_secret_configured": bool(settings.hostinger_mail_webhook_secret),
        "allowed_senders": settings.mailbox_allowed_senders_list,
    }


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {mailbox_api_key()}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            if isinstance(item, dict):
                addr = item.get("address") or item.get("email") or ""
                name = item.get("name") or ""
                if addr and name:
                    out.append(f"{name} <{addr}>")
                elif addr:
                    out.append(str(addr))
            elif str(item).strip():
                out.append(str(item))
        return out
    if isinstance(value, dict):
        return _as_list([value])
    return [str(value)]


def _clip(text: str | None) -> str:
    raw = text or ""
    if len(raw) <= _MAX_BODY:
        return raw
    return raw[:_MAX_BODY] + "\n\n[truncated]"


def _html_from_text(text: str) -> str:
    import html as html_lib

    safe = html_lib.escape(text or "").replace("\n", "<br>\n")
    return (
        "<div style=\"font-family:Segoe UI,Arial,sans-serif;font-size:15px;"
        f"line-height:1.55;color:#1a1210\">{safe}</div>"
    )


def _unwrap(body: Any) -> Any:
    if isinstance(body, dict) and "data" in body:
        return body["data"]
    return body


def _format_addr(value: Any) -> str:
    if isinstance(value, dict):
        addr = str(value.get("address") or value.get("email") or "").strip()
        name = str(value.get("name") or "").strip()
        if name and addr:
            return f"{name} <{addr}>"
        return addr or name
    return str(value or "").strip()


def sender_allowed(from_email: str) -> bool:
    allow = get_settings().mailbox_allowed_senders_list
    if not allow:
        return True
    addr = parse_email_addr(from_email)
    if addr in allow:
        return True
    domain = addr.split("@")[-1] if "@" in addr else ""
    return bool(domain and f"@{domain}" in allow)


def verify_hostinger_webhook(
    *,
    authorization: str,
    secret: str | None = None,
) -> bool:
    """Hostinger Mail webhooks send the secret as a Bearer token."""
    expected = (
        secret
        if secret is not None
        else get_settings().hostinger_mail_webhook_secret
    ) or ""
    if not expected:
        return False
    token = (authorization or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    if not token:
        return False
    return hmac.compare_digest(token, expected)


def provider_ref(folder: str, uid: Any) -> str:
    return f"{folder or _INBOX}:{uid}"


def parse_provider_ref(raw: str | None) -> tuple[str, str]:
    text = (raw or "").strip()
    if ":" in text:
        folder, uid = text.rsplit(":", 1)
        return folder or _INBOX, uid
    return _INBOX, text


def message_to_dict(row: m.MailboxMessage, *, include_body: bool = False) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": row.id,
        "direction": row.direction,
        "provider_email_id": row.provider_email_id,
        "from": row.from_email,
        "to": _load_list(row.to_emails),
        "cc": _load_list(row.cc_emails),
        "subject": row.subject,
        "status": row.status,
        "allowlisted": bool(row.allowlisted),
        "attachment_count": len(_load_list(row.attachments_json)),
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "read_at": row.read_at.isoformat() if row.read_at else None,
    }
    if include_body:
        data["text"] = row.text_body or ""
        data["html"] = row.html_body or ""
        data["rfc_message_id"] = row.rfc_message_id
        data["in_reply_to"] = row.in_reply_to
        data["attachments"] = _load_list(row.attachments_json)
        data["received_for"] = _load_list(row.received_for)
    return data


def _load_list(raw: str | None) -> list[Any]:
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def snapshot_for_riu(db: Session, *, limit: int = 8) -> dict[str, Any]:
    status = mailbox_status()
    if not status["configured"]:
        return {**status, "unread": 0, "recent": []}
    unread = (
        db.query(m.MailboxMessage)
        .filter_by(direction="inbound", status="unread")
        .count()
    )
    rows = (
        db.query(m.MailboxMessage)
        .order_by(m.MailboxMessage.created_at.desc())
        .limit(limit)
        .all()
    )
    return {
        **status,
        "unread": unread,
        "recent": [
            {
                "id": r.id,
                "direction": r.direction,
                "from": r.from_email,
                "to": _load_list(r.to_emails)[:3],
                "subject": r.subject,
                "status": r.status,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }


def list_messages(
    db: Session,
    *,
    unread_only: bool = False,
    limit: int = 40,
) -> list[m.MailboxMessage]:
    q = db.query(m.MailboxMessage).order_by(m.MailboxMessage.created_at.desc())
    if unread_only:
        q = q.filter_by(direction="inbound", status="unread")
    return q.limit(max(1, min(int(limit or 40), 100))).all()


def get_message(db: Session, message_id: str) -> m.MailboxMessage | None:
    return db.query(m.MailboxMessage).filter_by(id=message_id).first()


def mark_read(db: Session, row: m.MailboxMessage) -> m.MailboxMessage:
    if row.direction == "inbound" and row.status == "unread":
        row.status = "read"
        row.read_at = _now()
        db.commit()
        db.refresh(row)
    return row


def resolve_mailbox_id() -> str:
    settings = get_settings()
    configured = (settings.hostinger_mail_mailbox_id or "").strip()
    if configured:
        return configured
    if not mailbox_api_key():
        return ""
    try:
        resp = httpx.get(f"{MAIL_API}/api/v1/me", headers=_headers(), timeout=30.0)
    except Exception:  # noqa: BLE001
        logger.exception("Hostinger Mail account lookup failed")
        return ""
    if resp.status_code >= 400:
        logger.warning("Hostinger Mail /me HTTP %s", resp.status_code)
        return ""
    data = _unwrap(resp.json() if resp.content else {})
    boxes = []
    if isinstance(data, dict):
        boxes = data.get("mailboxes") or []
    elif isinstance(data, list):
        boxes = data
    wanted = mailbox_address()
    picked = ""
    for box in boxes:
        if not isinstance(box, dict):
            continue
        rid = str(box.get("resourceId") or box.get("resource_id") or "").strip()
        addr = parse_email_addr(str(box.get("address") or ""))
        if wanted and addr == wanted and rid:
            return rid
        if rid and not picked:
            picked = rid
    return picked


def _folder_url(mailbox_id: str, folder: str) -> str:
    enc = quote(folder or _INBOX, safe="")
    return f"{MAIL_API}/api/v1/mailboxes/{mailbox_id}/folders/{enc}"


def fetch_message_text(mailbox_id: str, folder: str, uid: str | int) -> dict[str, str]:
    resp = httpx.get(
        f"{_folder_url(mailbox_id, folder)}/messages/{uid}/text",
        headers=_headers(),
        timeout=30.0,
    )
    if resp.status_code >= 400:
        logger.warning("Hostinger message text HTTP %s", resp.status_code)
        return {"text": "", "html": ""}
    data = _unwrap(resp.json() if resp.content else {})
    if not isinstance(data, dict):
        return {"text": "", "html": ""}
    return {
        "text": str(data.get("text") or ""),
        "html": str(data.get("html") or ""),
    }


def list_remote_inbox(*, limit: int = 25) -> list[dict[str, Any]]:
    mailbox_id = resolve_mailbox_id()
    if not mailbox_id:
        return []
    resp = httpx.get(
        f"{_folder_url(mailbox_id, _INBOX)}/messages",
        headers=_headers(),
        params={"page": 1, "perPage": max(1, min(int(limit or 25), 100)), "sort": "-uid"},
        timeout=30.0,
    )
    if resp.status_code >= 400:
        logger.warning("Hostinger inbox list HTTP %s", resp.status_code)
        return []
    data = _unwrap(resp.json() if resp.content else {})
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        items = data.get("data") or data.get("messages") or data.get("items") or []
        return [x for x in items if isinstance(x, dict)]
    return []


def _upsert_inbound(
    db: Session,
    *,
    provider_id: str | None,
    from_email: str,
    to_emails: list[str],
    cc_emails: list[str],
    subject: str,
    text_body: str,
    html_body: str,
    rfc_message_id: str | None,
    attachments: list[Any],
    received_for: list[str],
    folder: str = _INBOX,
    status_override: str | None = None,
) -> m.MailboxMessage:
    existing = None
    if provider_id:
        existing = (
            db.query(m.MailboxMessage)
            .filter_by(provider_email_id=provider_id)
            .first()
        )
    allowed = sender_allowed(from_email)
    status = status_override or ("ignored" if not allowed else "unread")
    if existing:
        if text_body and not existing.text_body:
            existing.text_body = _clip(text_body)
        if html_body and not existing.html_body:
            existing.html_body = _clip(html_body)
        if rfc_message_id:
            existing.rfc_message_id = rfc_message_id
        if attachments:
            existing.attachments_json = json.dumps(attachments)
        db.commit()
        db.refresh(existing)
        return existing
    row = m.MailboxMessage(
        id=_uid(),
        direction="inbound",
        provider_email_id=provider_id,
        rfc_message_id=rfc_message_id,
        from_email=(from_email or "")[:320],
        to_emails=json.dumps(to_emails),
        cc_emails=json.dumps(cc_emails),
        subject=(subject or "")[:500],
        text_body=_clip(text_body),
        html_body=_clip(html_body),
        attachments_json=json.dumps(attachments or []),
        status=status,
        received_for=json.dumps(received_for or []),
        thread_key=(folder or _INBOX)[:255],
        allowlisted=allowed,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def ingest_received_event(db: Session, event: dict[str, Any]) -> m.MailboxMessage | None:
    """Store a Hostinger Mail message.received payload (or a synced message)."""
    data = event.get("message") if isinstance(event.get("message"), dict) else event
    if isinstance(event.get("data"), dict) and "uid" in event["data"]:
        data = event["data"]
    if not isinstance(data, dict):
        return None
    folder = str(data.get("folder") or data.get("path") or _INBOX)
    uid = data.get("uid") or data.get("id")
    provider_id = provider_ref(folder, uid) if uid not in (None, "") else None
    from_email = _format_addr(data.get("from") or data.get("sender") or "")
    to_emails = _as_list(data.get("to"))
    if not to_emails:
        mailbox_hint = event.get("mailbox") or mailbox_address()
        if mailbox_hint:
            to_emails = [str(mailbox_hint)]
    subject = str(data.get("subject") or "")
    text_body = str(data.get("text") or data.get("preview") or data.get("snippet") or "")
    html_body = str(data.get("html") or "")
    rfc_message_id = str(data.get("messageId") or data.get("message_id") or "") or None
    attachments = data.get("attachments") if isinstance(data.get("attachments"), list) else []
    received_for = _as_list(event.get("mailbox") or data.get("received_for"))

    if uid not in (None, "") and mailbox_api_key() and not (text_body or html_body):
        mailbox_id = resolve_mailbox_id()
        if mailbox_id:
            try:
                body = fetch_message_text(mailbox_id, folder, uid)
                text_body = body.get("text") or text_body
                html_body = body.get("html") or html_body
            except Exception:  # noqa: BLE001
                logger.exception("Failed to hydrate Hostinger message")

    if not provider_id and not from_email and not subject:
        return None
    return _upsert_inbound(
        db,
        provider_id=provider_id,
        from_email=from_email,
        to_emails=to_emails,
        cc_emails=_as_list(data.get("cc")),
        subject=subject,
        text_body=text_body,
        html_body=html_body,
        rfc_message_id=rfc_message_id,
        attachments=attachments,
        received_for=received_for,
        folder=folder,
    )


def sync_remote_inbox(db: Session, *, limit: int = 25) -> dict[str, Any]:
    remote = list_remote_inbox(limit=limit)
    added = 0
    for item in remote:
        before = db.query(m.MailboxMessage).count()
        ingest_received_event(db, item)
        after = db.query(m.MailboxMessage).count()
        if after > before:
            added += 1
    return {"ok": True, "remote": len(remote), "ingested": added}


def send_agent_email(
    db: Session,
    *,
    to: str,
    subject: str,
    body: str,
    in_reply_to: str | None = None,
    reply_folder: str | None = None,
    reply_uid: str | None = None,
    user_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    to_addr = parse_email_addr(to) or (to or "").strip()
    if not to_addr or "@" not in to_addr:
        return {"ok": False, "error": "Need a valid To address."}
    if not mailbox_configured():
        return {"ok": False, "error": "Riu mailbox is not configured."}
    mailbox_id = resolve_mailbox_id()
    if not mailbox_id:
        return {"ok": False, "error": "Hostinger mailbox id is not available."}

    display = mailbox_from()
    if "<" in display:
        display = display.split("<", 1)[0].strip() or "Riu"
    html = _html_from_text(body)
    payload: dict[str, Any] = {
        "to": [to_addr],
        "displayName": display[:80],
        "subject": (subject or "Message from Riu").strip()[:500],
        "text": body,
        "html": html,
    }
    if reply_uid:
        payload["inReplyTo"] = {
            "uid": int(reply_uid) if str(reply_uid).isdigit() else reply_uid,
            "folder": reply_folder or _INBOX,
        }

    log_id = _uid("em_")
    provider_id = None
    error = None
    status = "queued"
    try:
        resp = httpx.post(
            f"{MAIL_API}/api/v1/mailboxes/{mailbox_id}/send",
            headers=_headers(),
            json=payload,
            timeout=30.0,
        )
        if resp.status_code >= 400:
            status = "error"
            error = "Mailbox send failed"
            logger.error("Hostinger send HTTP %s: %s", resp.status_code, resp.text[:400])
        else:
            sent = _unwrap(resp.json() if resp.content else {})
            if isinstance(sent, dict):
                provider_id = str(
                    sent.get("uid")
                    or sent.get("id")
                    or sent.get("messageId")
                    or ""
                ) or None
            status = "sent"
    except Exception as exc:  # noqa: BLE001
        status = "error"
        error = str(exc)
        logger.exception("Hostinger mailbox send failed")

    if db is not None:
        db.add(
            m.EmailLog(
                id=log_id,
                to_email=to_addr,
                subject=payload["subject"],
                template="riu_mailbox",
                status=status,
                provider_id=provider_id,
                error=error,
            )
        )
        row = m.MailboxMessage(
            id=_uid(),
            direction="outbound",
            provider_email_id=provider_id,
            rfc_message_id=None,
            in_reply_to=in_reply_to,
            from_email=mailbox_from()[:320],
            to_emails=json.dumps([to_addr]),
            cc_emails="[]",
            subject=payload["subject"],
            text_body=_clip(body),
            html_body=_clip(html),
            attachments_json="[]",
            status="sent" if status == "sent" else "failed",
            received_for="[]",
            thread_key=(reply_folder or in_reply_to or provider_id or "")[:255] or None,
            allowlisted=True,
            created_by_user_id=user_id,
            riu_session_id=session_id,
            error=error,
        )
        db.add(row)
        try:
            db.commit()
            db.refresh(row)
        except Exception:  # noqa: BLE001
            db.rollback()
            return {"ok": False, "error": error or "Could not save sent mail."}
        if status != "sent":
            return {"ok": False, "error": error or "Mailbox send failed.", "id": row.id}
        return {"ok": True, "id": row.id, "provider_id": provider_id, "to": to_addr}

    if status != "sent":
        return {"ok": False, "error": error or "Mailbox send failed."}
    return {"ok": True, "provider_id": provider_id, "to": to_addr}


def reply_to_message(
    db: Session,
    *,
    row: m.MailboxMessage,
    body: str,
    user_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    if row.direction != "inbound":
        return {"ok": False, "error": "Can only reply to inbound mail."}
    to = parse_email_addr(row.from_email) or row.from_email
    subject = row.subject or ""
    if subject and not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    folder, uid = parse_provider_ref(row.provider_email_id)
    result = send_agent_email(
        db,
        to=to,
        subject=subject or "Re:",
        body=body,
        in_reply_to=row.rfc_message_id,
        reply_folder=folder,
        reply_uid=uid or None,
        user_id=user_id,
        session_id=session_id,
    )
    if result.get("ok"):
        row.status = "replied"
        if not row.read_at:
            row.read_at = _now()
        db.commit()
    return result


def hydrate_if_needed(db: Session, row: m.MailboxMessage) -> m.MailboxMessage:
    if row.direction != "inbound":
        return row
    if (row.text_body or row.html_body) or not row.provider_email_id:
        return row
    if not mailbox_api_key():
        return row
    mailbox_id = resolve_mailbox_id()
    if not mailbox_id:
        return row
    folder, uid = parse_provider_ref(row.provider_email_id)
    if not uid:
        return row
    try:
        body = fetch_message_text(mailbox_id, folder, uid)
    except Exception:  # noqa: BLE001
        logger.exception("hydrate Hostinger message failed")
        return row
    if body.get("text"):
        row.text_body = _clip(body["text"])
    if body.get("html"):
        row.html_body = _clip(body["html"])
    db.commit()
    db.refresh(row)
    return row
