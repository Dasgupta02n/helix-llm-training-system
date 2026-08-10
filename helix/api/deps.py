"""FastAPI dependencies: auth + tenant isolation."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from helix.db import models as m
from helix.db.session import get_db
from helix.security import decode_token

bearer = HTTPBearer(auto_error=False)


@dataclass
class AuthContext:
    user: m.User
    tenant: m.Tenant | None
    role: str | None  # membership role in current tenant


def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
) -> m.User:
    if not creds:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    payload = decode_token(creds.credentials)
    if not payload or "sub" not in payload:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    user = db.query(m.User).filter_by(id=payload["sub"], is_active=True).first()
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


def get_auth(
    tenant_slug: str | None = None,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> AuthContext:
    if not tenant_slug:
        return AuthContext(user=user, tenant=None, role=None)
    tenant = db.query(m.Tenant).filter_by(slug=tenant_slug, is_active=True).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    if user.is_superadmin:
        return AuthContext(user=user, tenant=tenant, role="owner")
    mem = (
        db.query(m.Membership)
        .filter_by(user_id=user.id, tenant_id=tenant.id)
        .first()
    )
    if not mem:
        raise HTTPException(status_code=403, detail="Not a member of this tenant")
    return AuthContext(user=user, tenant=tenant, role=mem.role)


def require_superadmin(user: m.User = Depends(get_current_user)) -> m.User:
    if not user.is_superadmin:
        raise HTTPException(status_code=403, detail="Superadmin required")
    return user
