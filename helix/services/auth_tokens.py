"""Secure one-time auth tokens (verify / reset / set password / invite)."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from helix.config import get_settings
from helix.db import models as m


def _uid(prefix: str = "tok_") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_auth_token(
    db: Session,
    user_id: str,
    purpose: str,
    hours: int | None = None,
    meta: str | None = None,
) -> str:
    """Create token row; returns raw token (show once to user/email)."""
    settings = get_settings()
    raw = secrets.token_urlsafe(32)
    exp_h = hours if hours is not None else settings.auth_token_expire_hours
    expires = datetime.now(timezone.utc) + timedelta(hours=exp_h)
    db.add(
        m.AuthToken(
            id=_uid(),
            user_id=user_id,
            token_hash=hash_token(raw),
            purpose=purpose,
            expires_at=expires,
            meta_json=meta,
        )
    )
    db.commit()
    return raw


def consume_auth_token(
    db: Session,
    raw: str,
    purpose: str | list[str] | None = None,
) -> m.AuthToken | None:
    """Validate and mark token used. Returns token row or None."""
    th = hash_token(raw)
    q = db.query(m.AuthToken).filter_by(token_hash=th, used_at=None)
    row = q.first()
    if not row:
        return None
    if purpose is not None:
        allowed = [purpose] if isinstance(purpose, str) else list(purpose)
        if row.purpose not in allowed:
            return None
    now = datetime.now(timezone.utc)
    exp = row.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if exp < now:
        return None
    row.used_at = now
    db.commit()
    return row


def invalidate_tokens(db: Session, user_id: str, purpose: str) -> None:
    now = datetime.now(timezone.utc)
    rows = (
        db.query(m.AuthToken)
        .filter_by(user_id=user_id, purpose=purpose, used_at=None)
        .all()
    )
    for r in rows:
        r.used_at = now
    db.commit()
