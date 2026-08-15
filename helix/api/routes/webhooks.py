"""Inbound webhooks (Hostinger Agentic Mail)."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from helix.config import get_settings
from helix.db.session import get_db
from helix.services import mailbox as mailbox_svc

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


@router.post("/hostinger-mail")
async def hostinger_mail_webhook(
    request: Request, db: Session = Depends(get_db)
) -> JSONResponse:
    settings = get_settings()
    if settings.hostinger_mail_webhook_secret:
        auth = request.headers.get("authorization") or request.headers.get(
            "x-webhook-secret"
        ) or ""
        if not mailbox_svc.verify_hostinger_webhook(authorization=auth):
            return JSONResponse({"ok": False}, status_code=401)

    raw = await request.body()
    try:
        event = json.loads(raw.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return JSONResponse({"ok": False}, status_code=400)
    if not isinstance(event, dict):
        return JSONResponse({"ok": True})

    etype = str(event.get("event") or event.get("type") or "")
    if etype in {"message.received", "email.received", ""}:
        try:
            mailbox_svc.ingest_received_event(db, event)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to ingest inbound Hostinger mail")
            return JSONResponse({"ok": False, "ingested": False})
    return JSONResponse({"ok": True})
