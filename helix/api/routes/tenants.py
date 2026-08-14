from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from helix.api.deps import get_current_user, require_superadmin
from helix.api.schemas import MemberAdd, TenantCreate, TenantOut
from helix.config import get_settings
from helix.db import models as m
from helix.db.bootstrap import seed_tenant_data
from helix.db.session import get_db
from helix.security import hash_password

router = APIRouter(prefix="/api/tenants", tags=["tenants"])


def _uid(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


@router.get("", response_model=list[TenantOut])
def list_tenants(
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[m.Tenant]:
    if user.is_superadmin:
        return db.query(m.Tenant).order_by(m.Tenant.created_at.desc()).all()
    rows = (
        db.query(m.Tenant)
        .join(m.Membership, m.Membership.tenant_id == m.Tenant.id)
        .filter(m.Membership.user_id == user.id)
        .all()
    )
    return rows


@router.post("", response_model=TenantOut)
def create_tenant(
    body: TenantCreate,
    admin: m.User = Depends(require_superadmin),
    db: Session = Depends(get_db),
) -> m.Tenant:
    if db.query(m.Tenant).filter_by(slug=body.slug).first():
        raise HTTPException(400, "Slug already exists")
    settings = get_settings()
    tenant = m.Tenant(
        id=_uid("ten_"),
        slug=body.slug,
        name=body.name,
        plan=body.plan,
        monthly_budget_usd=body.monthly_budget_usd
        or settings.default_tenant_monthly_budget_usd,
        openrouter_api_key=body.openrouter_api_key,
        openrouter_model=body.openrouter_model,
    )
    db.add(tenant)
    db.flush()

    owner = admin
    if body.owner_email:
        owner = db.query(m.User).filter_by(email=body.owner_email.lower()).first()
        if not owner:
            if not body.owner_password:
                raise HTTPException(400, "owner_password required for new user")
            owner = m.User(
                id=_uid("usr_"),
                email=body.owner_email.lower(),
                hashed_password=hash_password(body.owner_password),
                full_name=body.name + " Owner",
                email_verified=True,
                admin_approved=True,
                password_set=True,
                is_active=True,
            )
            db.add(owner)
            db.flush()

    db.add(
        m.Membership(
            id=_uid("mem_"),
            user_id=owner.id,
            tenant_id=tenant.id,
            role="owner",
        )
    )
    seed_tenant_data(db, tenant.id)
    db.commit()
    db.refresh(tenant)
    return tenant


@router.get("/{slug}", response_model=TenantOut)
def get_tenant(
    slug: str,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> m.Tenant:
    tenant = db.query(m.Tenant).filter_by(slug=slug).first()
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    if not user.is_superadmin:
        mem = (
            db.query(m.Membership)
            .filter_by(user_id=user.id, tenant_id=tenant.id)
            .first()
        )
        if not mem:
            raise HTTPException(403, "Forbidden")
    return tenant


@router.post("/{slug}/members")
def add_member(
    slug: str,
    body: MemberAdd,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    tenant = db.query(m.Tenant).filter_by(slug=slug).first()
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    mem = (
        db.query(m.Membership)
        .filter_by(user_id=user.id, tenant_id=tenant.id)
        .first()
    )
    if not user.is_superadmin and (not mem or mem.role not in {"owner", "admin"}):
        raise HTTPException(403, "Admin role required")

    target = db.query(m.User).filter_by(email=body.email.lower()).first()
    if not target:
        if not body.password:
            raise HTTPException(400, "password required for new user")
        target = m.User(
            id=_uid("usr_"),
            email=body.email.lower(),
            hashed_password=hash_password(body.password),
            full_name=body.full_name,
            email_verified=True,
            admin_approved=True,
            password_set=True,
            is_active=True,
        )
        db.add(target)
        db.flush()

    if db.query(m.Membership).filter_by(user_id=target.id, tenant_id=tenant.id).first():
        raise HTTPException(400, "Already a member")

    db.add(
        m.Membership(
            id=_uid("mem_"),
            user_id=target.id,
            tenant_id=tenant.id,
            role=body.role if body.role in {"owner", "admin", "member"} else "member",
        )
    )
    db.commit()
    return {"ok": True, "user_id": target.id, "tenant": tenant.slug, "role": body.role}
