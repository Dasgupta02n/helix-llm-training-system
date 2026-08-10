"""Auth helpers: password hashing and JWT."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from jose import JWTError, jwt
from passlib.context import CryptContext

from helix.config import get_settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
ALGORITHM = "HS256"


def hash_password(password: str) -> str:
    return pwd_context.hash(password[:72])


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain[:72], hashed)


def create_access_token(subject: str, extra: dict[str, Any] | None = None) -> str:
    settings = get_settings()
    now = datetime.now(timezone.utc)
    expire_minutes = settings.access_token_expire_minutes
    if settings.is_production and expire_minutes > 60 * 24 * 2:
        # Cap very long-lived tokens in production unless explicitly short
        expire_minutes = min(expire_minutes, 60 * 24)  # max 24h default cap
    expire = now + timedelta(minutes=expire_minutes)
    payload: dict[str, Any] = {
        "sub": subject,
        "exp": expire,
        "iat": now,
        "typ": "access",
    }
    if extra:
        # never allow callers to override typ/exp accidentally
        extra = {k: v for k, v in extra.items() if k not in {"exp", "typ", "iat"}}
        payload.update(extra)
    return jwt.encode(payload, settings.helix_secret_key, algorithm=ALGORITHM)


def decode_token(token: str) -> dict[str, Any] | None:
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.helix_secret_key,
            algorithms=[ALGORITHM],
            options={"require_exp": True, "require_sub": True},
        )
        # Reject non-access tokens if typ is present
        if payload.get("typ") not in (None, "access"):
            return None
        return payload
    except JWTError:
        return None
