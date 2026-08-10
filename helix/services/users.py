"""User + workspace provisioning helpers."""

from __future__ import annotations

import re
import uuid

from sqlalchemy.orm import Session

from helix.config import get_settings
from helix.db import models as m
from helix.db.bootstrap import seed_tenant_data
from helix.security import hash_password


def _uid(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s or "workspace")[:48]


def unique_tenant_slug(db: Session, base: str) -> str:
    slug = slugify(base)
    if not db.query(m.Tenant).filter_by(slug=slug).first():
        return slug
    for i in range(2, 1000):
        candidate = f"{slug}-{i}"
        if not db.query(m.Tenant).filter_by(slug=candidate).first():
            return candidate
    return f"{slug}-{uuid.uuid4().hex[:6]}"


def create_user(
    db: Session,
    *,
    email: str,
    full_name: str = "",
    password: str | None = None,
    is_superadmin: bool = False,
    email_verified: bool = False,
    password_set: bool | None = None,
    is_active: bool = True,
) -> m.User:
    email = email.lower().strip()
    if db.query(m.User).filter_by(email=email).first():
        raise ValueError("An account with this email already exists")

    has_pw = bool(password)
    user = m.User(
        id=_uid("usr_"),
        email=email,
        full_name=(full_name or "").strip(),
        hashed_password=hash_password(password) if has_pw else hash_password(secrets_unusable()),
        is_superadmin=is_superadmin,
        is_active=is_active,
        email_verified=email_verified,
        password_set=password_set if password_set is not None else has_pw,
    )
    db.add(user)
    db.flush()
    return user


def secrets_unusable() -> str:
    import secrets

    return secrets.token_urlsafe(48)


def create_personal_workspace(db: Session, user: m.User) -> m.Tenant:
    settings = get_settings()
    base = user.full_name or user.email.split("@")[0]
    slug = unique_tenant_slug(db, f"{base}-workspace")
    tenant = m.Tenant(
        id=_uid("ten_"),
        slug=slug,
        name=f"{user.full_name or user.email.split('@')[0]}'s workspace",
        plan="starter",
        monthly_budget_usd=settings.default_tenant_monthly_budget_usd,
    )
    db.add(tenant)
    db.flush()
    db.add(
        m.Membership(
            id=_uid("mem_"),
            user_id=user.id,
            tenant_id=tenant.id,
            role="owner",
        )
    )
    seed_tenant_data(db, tenant.id)
    return tenant


def user_public(user: m.User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "full_name": user.full_name,
        "is_superadmin": user.is_superadmin,
        "is_active": user.is_active,
        "email_verified": bool(user.email_verified),
        "password_set": bool(user.password_set),
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
        "created_at": user.created_at.isoformat() if user.created_at else None,
    }
