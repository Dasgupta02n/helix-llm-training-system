"""Riu conversational helper API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from helix.api.deps import get_current_user
from helix.db import models as m
from helix.db.session import get_db
from helix.services import riu as riu_service

router = APIRouter(prefix="/api/t/{slug}/riu", tags=["riu"])


def _tenant_for(user: m.User, slug: str, db: Session) -> m.Tenant:
    tenant = db.query(m.Tenant).filter_by(slug=slug, is_active=True).first()
    if not tenant:
        raise HTTPException(404, "Workspace not found")
    if not user.is_superadmin:
        mem = (
            db.query(m.Membership)
            .filter_by(user_id=user.id, tenant_id=tenant.id)
            .first()
        )
        if not mem:
            raise HTTPException(403, "Forbidden")
    return tenant


class RiuMessageIn(BaseModel):
    message: str = Field(min_length=1, max_length=8000)


@router.get("/session")
def get_session(
    slug: str,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    tenant = _tenant_for(user, slug, db)
    row = riu_service.get_or_create_session(
        db, user_id=user.id, tenant_id=tenant.id
    )
    return riu_service.session_to_dict(row)


@router.post("/session")
def new_session(
    slug: str,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    tenant = _tenant_for(user, slug, db)
    row = riu_service.create_session(db, user_id=user.id, tenant_id=tenant.id)
    return riu_service.session_to_dict(row)


@router.post("/message")
def post_message(
    slug: str,
    body: RiuMessageIn,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    tenant = _tenant_for(user, slug, db)
    row = riu_service.get_or_create_session(
        db, user_id=user.id, tenant_id=tenant.id
    )
    try:
        return riu_service.handle_user_message(
            db, tenant=tenant, user=user, session=row, text=body.message
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Riu error: {e}") from e
