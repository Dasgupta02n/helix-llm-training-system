"""Admin user management + workspace invites."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from helix.api.deps import get_current_user, require_superadmin
from helix.config import get_settings
from helix.db import models as m
from helix.db.session import get_db
from helix.services.auth_tokens import create_auth_token, invalidate_tokens
from helix.services.email import send_invite_email, send_set_password_email
from helix.services.users import create_user, user_public

router = APIRouter(prefix="/api/users", tags=["users"])


class InviteRequest(BaseModel):
    email: str
    full_name: str = ""
    role: str = "member"  # for tenant invite
    tenant_slug: str | None = None
    is_superadmin: bool = False


class UserUpdate(BaseModel):
    full_name: str | None = None
    is_active: bool | None = None
    is_superadmin: bool | None = None
    email_verified: bool | None = None


def _app_link(mode: str, token: str) -> str:
    settings = get_settings()
    return f"{settings.helix_base_url.rstrip('/')}/app?mode={mode}&token={token}"


@router.get("")
def list_users(
    admin: m.User = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> list[dict]:
    rows = db.query(m.User).order_by(m.User.created_at.desc()).all()
    return [user_public(u) for u in rows]


@router.get("/{user_id}")
def get_user(
    user_id: str,
    admin: m.User = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> dict:
    user = db.query(m.User).filter_by(id=user_id).first()
    if not user:
        raise HTTPException(404, "User not found")
    data = user_public(user)
    mems = (
        db.query(m.Membership, m.Tenant)
        .join(m.Tenant, m.Tenant.id == m.Membership.tenant_id)
        .filter(m.Membership.user_id == user.id)
        .all()
    )
    data["tenants"] = [
        {"slug": t.slug, "name": t.name, "role": mem.role} for mem, t in mems
    ]
    return data


@router.patch("/{user_id}")
def update_user(
    user_id: str,
    body: UserUpdate,
    admin: m.User = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> dict:
    user = db.query(m.User).filter_by(id=user_id).first()
    if not user:
        raise HTTPException(404, "User not found")
    if user.id == admin.id and body.is_active is False:
        raise HTTPException(400, "You cannot deactivate yourself")
    if body.full_name is not None:
        user.full_name = body.full_name.strip()
    if body.is_active is not None:
        user.is_active = body.is_active
    if body.is_superadmin is not None:
        user.is_superadmin = body.is_superadmin
    if body.email_verified is not None:
        user.email_verified = body.email_verified
    user.updated_at = datetime.now(timezone.utc)
    db.commit()
    return user_public(user)


@router.post("/invite")
def invite_user(
    body: InviteRequest,
    admin: m.User = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> dict:
    """Invite a user by email — they receive a set-password link via Resend."""
    settings = get_settings()
    email = body.email.lower().strip()
    existing = db.query(m.User).filter_by(email=email).first()
    tenant = None
    if body.tenant_slug:
        tenant = db.query(m.Tenant).filter_by(slug=body.tenant_slug).first()
        if not tenant:
            raise HTTPException(404, "Workspace not found")

    if existing:
        user = existing
        if not user.is_active:
            user.is_active = True
    else:
        try:
            user = create_user(
                db,
                email=email,
                full_name=body.full_name,
                password=None,
                is_superadmin=body.is_superadmin,
                email_verified=False,
                password_set=False,
            )
        except ValueError as e:
            raise HTTPException(400, str(e)) from e

    if tenant:
        mem = (
            db.query(m.Membership)
            .filter_by(user_id=user.id, tenant_id=tenant.id)
            .first()
        )
        if not mem:
            import uuid

            db.add(
                m.Membership(
                    id=f"mem_{uuid.uuid4().hex[:12]}",
                    user_id=user.id,
                    tenant_id=tenant.id,
                    role=body.role if body.role in {"owner", "admin", "member"} else "member",
                )
            )
    db.commit()

    invalidate_tokens(db, user.id, "invite")
    invalidate_tokens(db, user.id, "set_password")
    raw = create_auth_token(db, user.id, "invite")
    link = _app_link("set-password", raw)
    workspace_name = tenant.name if tenant else "Helix"
    result = send_invite_email(
        db,
        to=user.email,
        name=user.full_name,
        inviter=admin.full_name or admin.email,
        workspace=workspace_name,
        link=link,
    )
    # Also send set-password style if invite template fails visually — invite covers both
    if result.get("skipped"):
        send_set_password_email(db, user.email, user.full_name, link)

    return {
        "ok": True,
        "user": user_public(user),
        "message": "Invitation sent." if result.get("ok") else "User created; email may be skipped without RESEND_API_KEY.",
        "dev_link": link if (result.get("skipped") and not settings.is_production) else None,
    }


@router.post("/{user_id}/send-password-link")
def send_password_link(
    user_id: str,
    admin: m.User = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> dict:
    settings = get_settings()
    user = db.query(m.User).filter_by(id=user_id).first()
    if not user:
        raise HTTPException(404, "User not found")
    invalidate_tokens(db, user.id, "set_password")
    raw = create_auth_token(db, user.id, "set_password")
    link = _app_link("set-password", raw)
    result = send_set_password_email(db, user.email, user.full_name, link)
    return {
        "ok": True,
        "message": "Password setup email sent." if result.get("ok") else "Email skipped (no Resend key).",
        "dev_link": link if (result.get("skipped") and not settings.is_production) else None,
    }
