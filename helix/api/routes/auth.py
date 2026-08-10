"""Authentication: register, login, verify email, password set/reset (Resend)."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from helix.api.deps import get_current_user
from helix.api.schemas import LoginRequest, TokenResponse
from helix.api.security_middleware import is_valid_email, password_policy_ok
from helix.config import get_settings
from helix.db import models as m
from helix.db.session import get_db
from helix.security import create_access_token, hash_password, verify_password
from helix.services.auth_tokens import (
    create_auth_token,
    consume_auth_token,
    invalidate_tokens,
)
from helix.services.email import (
    send_password_reset_email,
    send_set_password_email,
    send_verification_email,
)
from helix.services.users import create_personal_workspace, create_user, user_public

router = APIRouter(prefix="/api/auth", tags=["auth"])


class RegisterRequest(BaseModel):
    email: str
    full_name: str = ""
    password: str = Field(min_length=8, max_length=128)


class MessageResponse(BaseModel):
    ok: bool = True
    message: str
    # Present only in local/dev when email was skipped — never in production responses ideally
    dev_link: str | None = None


class ForgotPasswordRequest(BaseModel):
    email: str


class TokenPasswordRequest(BaseModel):
    token: str
    password: str = Field(min_length=8, max_length=128)


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=128)


class ProfileUpdate(BaseModel):
    full_name: str | None = None


class ResendVerificationRequest(BaseModel):
    email: str


def _app_link(path: str, token: str) -> str:
    settings = get_settings()
    base = settings.helix_base_url.rstrip("/")
    return f"{base}/app?mode={path}&token={token}"


def _issue_login(user: m.User) -> TokenResponse:
    token = create_access_token(
        user.id,
        {"email": user.email, "sa": bool(user.is_superadmin)},
    )
    return TokenResponse(
        access_token=token,
        user_id=user.id,
        email=user.email,
        is_superadmin=user.is_superadmin,
    )


@router.post("/register", response_model=MessageResponse)
def register(body: RegisterRequest, db: Session = Depends(get_db)) -> MessageResponse:
    settings = get_settings()
    if not settings.allow_public_signup:
        raise HTTPException(403, "Public signup is disabled. Ask an admin for an invite.")

    email = body.email.lower().strip()
    if not is_valid_email(email):
        raise HTTPException(400, "Enter a valid email address")
    ok_pw, pw_msg = password_policy_ok(body.password)
    if not ok_pw:
        raise HTTPException(400, pw_msg)

    try:
        user = create_user(
            db,
            email=email,
            full_name=body.full_name,
            password=body.password,
            email_verified=not settings.require_email_verification,
            password_set=True,
        )
        if settings.auto_create_workspace_on_signup:
            create_personal_workspace(db, user)
        db.commit()
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    dev_link = None
    if settings.require_email_verification and not user.email_verified:
        invalidate_tokens(db, user.id, "verify_email")
        raw = create_auth_token(db, user.id, "verify_email")
        link = _app_link("verify", raw)
        result = send_verification_email(db, user.email, user.full_name, link)
        if result.get("skipped"):
            dev_link = link  # local without Resend
        return MessageResponse(
            message=(
                "Account created. Check your email to confirm your address before signing in."
            ),
            dev_link=dev_link if not settings.is_production else None,
        )

    return MessageResponse(message="Account created. You can sign in now.")


# Dummy bcrypt hash so missing-user logins still do a verify (mitigate timing leaks)
_DUMMY_HASH = hash_password("not-a-real-password-timing-pad")


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    settings = get_settings()
    email = body.email.lower().strip()
    user = db.query(m.User).filter_by(email=email).first()
    # Always run a password verify to reduce user-enumeration timing differences
    hashed = (
        user.hashed_password
        if user and user.password_set and user.hashed_password
        else _DUMMY_HASH
    )
    valid = verify_password(body.password, hashed)
    if not user or not user.is_active or not user.password_set or not user.hashed_password:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not valid:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if settings.require_email_verification and not user.email_verified and not user.is_superadmin:
        raise HTTPException(
            status_code=403,
            detail="Please confirm your email before signing in. Check your inbox.",
        )
    user.last_login_at = datetime.now(timezone.utc)
    db.commit()
    return _issue_login(user)


@router.get("/me")
def me(
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    memberships = (
        db.query(m.Membership, m.Tenant)
        .join(m.Tenant, m.Tenant.id == m.Membership.tenant_id)
        .filter(m.Membership.user_id == user.id)
        .all()
    )
    data = user_public(user)
    data["tenants"] = [
        {
            "id": t.id,
            "slug": t.slug,
            "name": t.name,
            "role": mem.role,
            "plan": t.plan,
        }
        for mem, t in memberships
    ]
    return data


@router.patch("/me")
def update_me(
    body: ProfileUpdate,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    if body.full_name is not None:
        user.full_name = body.full_name.strip()
        user.updated_at = datetime.now(timezone.utc)
        db.commit()
    return user_public(user)


@router.post("/change-password", response_model=MessageResponse)
def change_password(
    body: ChangePasswordRequest,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> MessageResponse:
    if not verify_password(body.current_password, user.hashed_password):
        raise HTTPException(400, "Current password is incorrect")
    ok_pw, pw_msg = password_policy_ok(body.new_password)
    if not ok_pw:
        raise HTTPException(400, pw_msg)
    user.hashed_password = hash_password(body.new_password)
    user.password_set = True
    user.updated_at = datetime.now(timezone.utc)
    db.commit()
    return MessageResponse(message="Password updated.")


@router.post("/forgot-password", response_model=MessageResponse)
def forgot_password(
    body: ForgotPasswordRequest, db: Session = Depends(get_db)
) -> MessageResponse:
    """Always returns success to avoid email enumeration."""
    settings = get_settings()
    user = db.query(m.User).filter_by(email=body.email.lower().strip(), is_active=True).first()
    dev_link = None
    if user:
        invalidate_tokens(db, user.id, "reset_password")
        raw = create_auth_token(db, user.id, "reset_password")
        link = _app_link("reset", raw)
        result = send_password_reset_email(db, user.email, user.full_name, link)
        if result.get("skipped"):
            dev_link = link
    return MessageResponse(
        message="If that email is registered, you’ll receive a reset link shortly.",
        dev_link=dev_link if (dev_link and not settings.is_production) else None,
    )


@router.post("/reset-password", response_model=MessageResponse)
def reset_password(
    body: TokenPasswordRequest, db: Session = Depends(get_db)
) -> MessageResponse:
    ok_pw, pw_msg = password_policy_ok(body.password)
    if not ok_pw:
        raise HTTPException(400, pw_msg)
    token = consume_auth_token(
        db, body.token, purpose=["reset_password", "set_password", "invite"]
    )
    if not token:
        raise HTTPException(400, "This link is invalid or has expired. Request a new one.")
    user = db.query(m.User).filter_by(id=token.user_id).first()
    if not user or not user.is_active:
        raise HTTPException(400, "Account not found")
    user.hashed_password = hash_password(body.password)
    user.password_set = True
    user.email_verified = True
    user.updated_at = datetime.now(timezone.utc)
    db.commit()
    return MessageResponse(message="Password saved. You can sign in now.")


@router.post("/set-password", response_model=MessageResponse)
def set_password(
    body: TokenPasswordRequest, db: Session = Depends(get_db)
) -> MessageResponse:
    ok_pw, pw_msg = password_policy_ok(body.password)
    if not ok_pw:
        raise HTTPException(400, pw_msg)
    token = consume_auth_token(
        db, body.token, purpose=["set_password", "invite", "reset_password"]
    )
    if not token:
        raise HTTPException(400, "This link is invalid or has expired.")
    user = db.query(m.User).filter_by(id=token.user_id).first()
    if not user:
        raise HTTPException(400, "Account not found")
    user.hashed_password = hash_password(body.password)
    user.password_set = True
    user.email_verified = True
    user.is_active = True
    user.updated_at = datetime.now(timezone.utc)
    db.commit()
    return MessageResponse(message="Password created. You can sign in now.")


@router.get("/verify-email", response_model=MessageResponse)
def verify_email(
    token: str = Query(...),
    db: Session = Depends(get_db),
) -> MessageResponse:
    row = consume_auth_token(db, token, purpose="verify_email")
    if not row:
        raise HTTPException(400, "This confirmation link is invalid or has expired.")
    user = db.query(m.User).filter_by(id=row.user_id).first()
    if not user:
        raise HTTPException(400, "Account not found")
    user.email_verified = True
    user.updated_at = datetime.now(timezone.utc)
    db.commit()
    return MessageResponse(message="Email confirmed. You can sign in now.")


@router.post("/resend-verification", response_model=MessageResponse)
def resend_verification(
    body: ResendVerificationRequest, db: Session = Depends(get_db)
) -> MessageResponse:
    settings = get_settings()
    user = db.query(m.User).filter_by(email=body.email.lower().strip()).first()
    dev_link = None
    if user and user.is_active and not user.email_verified:
        invalidate_tokens(db, user.id, "verify_email")
        raw = create_auth_token(db, user.id, "verify_email")
        link = _app_link("verify", raw)
        result = send_verification_email(db, user.email, user.full_name, link)
        if result.get("skipped"):
            dev_link = link
    return MessageResponse(
        message="If that account needs verification, we sent a new email.",
        dev_link=dev_link if (dev_link and not settings.is_production) else None,
    )


@router.get("/config")
def auth_public_config() -> dict:
    """Non-secret flags for the frontend."""
    settings = get_settings()
    return {
        "allow_public_signup": settings.allow_public_signup,
        "require_email_verification": settings.require_email_verification,
        "resend_configured": bool(settings.resend_api_key),
    }
