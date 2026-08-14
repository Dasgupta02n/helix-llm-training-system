"""Ownership / liability declaration for Double Helix trained-model download.

Acceptances are retained by email. There is no delete path. Account deletion
does not remove these rows.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from helix.db import models as m

DECLARATION_VERSION = "dh-liability-v1"

DECLARATION_TITLE = "Double Helix trained-model ownership and liability declaration"

DECLARATION_TEXT = """DOUBLE HELIX TRAINED-MODEL OWNERSHIP AND LIABILITY DECLARATION
Version: dh-liability-v1

By downloading this trained model package (QLoRA adapter, tokenizer files, and
related artifacts), you declare and agree:

1. OWNERSHIP
   You own the fine-tuned adapter and any model you create from it using gold
   that was in your Helix account. Helix does not claim copyright in your
   examples or in the adapter produced from them.

2. YOUR RESPONSIBILITY
   You are solely responsible for how you use, deploy, distribute, evaluate,
   or rely on this model. That includes hiring, credit, medical, legal,
   safety-critical, or any other high-risk use. Human review remains required.

3. NO LIABILITY
   Helix, Double Helix, Riu, c7x AI, and their operators, officers, and
   contractors are not liable for any claim, loss, damage, decision, or
   outcome arising from this model or its use — including direct, indirect,
   incidental, special, or consequential damages — to the maximum extent
   permitted by law.

4. NO WARRANTY
   The package is provided as-is. There is no warranty of accuracy, fitness
   for a particular purpose, non-infringement, or uninterrupted operation.

5. BASE MODEL LICENSE
   The public base checkpoint remains under its Apache-2.0 or MIT card. You
   must comply with that license. Helix does not redistribute base weights.

6. RECORD KEEPING
   A copy of this accepted declaration is emailed to you and stored in your
   Helix account. You cannot delete that copy. If you delete your account,
   Helix still retains this declaration in a backend record keyed by your
   email address.

I have read this declaration and I accept it."""


def _uid() -> str:
    return f"dcl_{uuid.uuid4().hex[:12]}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def declaration_payload() -> dict[str, str]:
    return {
        "version": DECLARATION_VERSION,
        "title": DECLARATION_TITLE,
        "text": DECLARATION_TEXT,
    }


def acceptance_to_dict(row: m.LiabilityAcceptance) -> dict[str, Any]:
    return {
        "id": row.id,
        "email": row.email,
        "train_job_id": row.train_job_id,
        "declaration_version": row.declaration_version,
        "declaration_text": row.declaration_text,
        "accepted_at": row.accepted_at.isoformat() if row.accepted_at else None,
        "email_status": row.email_status,
        "can_delete": False,
        "account_deleted_at": (
            row.account_deleted_at.isoformat() if row.account_deleted_at else None
        ),
    }


def get_acceptance(
    db: Session, *, email: str, train_job_id: str
) -> m.LiabilityAcceptance | None:
    em = (email or "").strip().lower()
    return (
        db.query(m.LiabilityAcceptance)
        .filter_by(
            email=em,
            train_job_id=train_job_id,
            declaration_version=DECLARATION_VERSION,
        )
        .first()
    )


def list_acceptances_for_user(
    db: Session, *, email: str, owner_user_id: str | None = None
) -> list[m.LiabilityAcceptance]:
    em = (email or "").strip().lower()
    q = db.query(m.LiabilityAcceptance).filter(
        m.LiabilityAcceptance.email == em
    )
    if owner_user_id:
        q = q.filter(
            (m.LiabilityAcceptance.owner_user_id == owner_user_id)
            | (m.LiabilityAcceptance.email == em)
        )
    return q.order_by(m.LiabilityAcceptance.accepted_at.desc()).all()


def accept_declaration(
    db: Session,
    *,
    user: m.User,
    tenant_id: str,
    train_job_id: str,
    confirm: bool,
    ip_address: str = "",
    user_agent: str = "",
) -> m.LiabilityAcceptance:
    if not confirm:
        raise ValueError(
            "You must accept the ownership and liability declaration "
            "(confirm=true) before the trained model can be downloaded."
        )
    email = (user.email or "").strip().lower()
    if not email:
        raise ValueError("Account has no email; cannot record a declaration.")
    existing = get_acceptance(db, email=email, train_job_id=train_job_id)
    if existing:
        if existing.email_status != "sent":
            _email_copy(db, existing, name=user.full_name or "")
        return existing

    row = m.LiabilityAcceptance(
        id=_uid(),
        email=email,
        owner_user_id=user.id,
        tenant_id=tenant_id,
        train_job_id=train_job_id,
        declaration_version=DECLARATION_VERSION,
        declaration_text=DECLARATION_TEXT,
        accepted_at=_now(),
        ip_address=(ip_address or "")[:80] or None,
        user_agent=(user_agent or "")[:500] or None,
        email_status="pending",
    )
    db.add(row)
    try:
        db.commit()
    except Exception:
        db.rollback()
        existing = get_acceptance(db, email=email, train_job_id=train_job_id)
        if existing:
            return existing
        raise
    db.refresh(row)
    _email_copy(db, row, name=user.full_name or "")
    return row


def retain_after_account_delete(db: Session, *, user: m.User) -> int:
    """Keep every declaration; only mark that the account was removed."""
    email = (user.email or "").strip().lower()
    now = _now()
    rows = (
        db.query(m.LiabilityAcceptance)
        .filter(
            (m.LiabilityAcceptance.owner_user_id == user.id)
            | (m.LiabilityAcceptance.email == email)
        )
        .all()
    )
    for row in rows:
        row.account_deleted_at = row.account_deleted_at or now
        row.email = email or row.email
        # Keep owner_user_id as a historical pointer; row is not deleted.
    db.commit()
    return len(rows)


def _email_copy(db: Session, row: m.LiabilityAcceptance, *, name: str) -> None:
    from helix.services.email import send_declaration_copy_email

    result = send_declaration_copy_email(
        db,
        to=row.email,
        name=name,
        accepted_at=row.accepted_at.isoformat() if row.accepted_at else "",
        job_id=row.train_job_id or "",
        version=row.declaration_version,
        text=row.declaration_text,
    )
    if result.get("ok"):
        row.email_status = "sent"
    elif result.get("skipped"):
        row.email_status = "skipped"
    else:
        row.email_status = "error"
    row.email_log_id = result.get("log_id")
    db.commit()
