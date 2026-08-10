"""HTTP security middleware: headers, rate limits, basic request guards."""

from __future__ import annotations

import re
import threading
import time
from collections import defaultdict, deque
from typing import Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from helix.config import get_settings

# path prefixes that are auth-sensitive (stricter rate limits)
_AUTH_SENSITIVE = (
    "/api/auth/login",
    "/api/auth/register",
    "/api/auth/forgot-password",
    "/api/auth/reset-password",
    "/api/auth/set-password",
    "/api/auth/resend-verification",
)


class RateLimiter:
    """In-memory sliding-window limiter (per-process; fine for single-worker VPS)."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, *, limit: int, window_sec: float) -> bool:
        now = time.monotonic()
        cutoff = now - window_sec
        with self._lock:
            q = self._hits[key]
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            # opportunistic cleanup of idle keys
            if len(self._hits) > 10_000:
                stale = [k for k, v in self._hits.items() if not v or v[-1] < cutoff]
                for k in stale[:5000]:
                    self._hits.pop(k, None)
            return True


_limiter = RateLimiter()


def client_ip(request: Request) -> str:
    """Best-effort client IP (respect first X-Forwarded-For hop from reverse proxy)."""
    settings = get_settings()
    if settings.trust_proxy_headers:
        xff = request.headers.get("x-forwarded-for") or ""
        if xff:
            return xff.split(",")[0].strip()[:64]
        real = request.headers.get("x-real-ip")
        if real:
            return real.strip()[:64]
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        response = await call_next(request)
        settings = get_settings()
        # Baseline headers for all responses
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=()",
        )
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        # CSP: app is mostly same-origin + Google Fonts + inline-free static
        csp = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com data:; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
        response.headers.setdefault("Content-Security-Policy", csp)
        if settings.is_production:
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
        # Avoid caching authenticated API responses by default
        if request.url.path.startswith("/api/") and "Cache-Control" not in response.headers:
            if request.url.path not in {"/api/health", "/api/auth/config"}:
                response.headers["Cache-Control"] = "no-store"
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        settings = get_settings()
        if not settings.rate_limit_enabled:
            return await call_next(request)

        path = request.url.path
        method = request.method.upper()
        ip = client_ip(request)

        # Global soft cap
        if not _limiter.allow(f"g:{ip}", limit=settings.rate_limit_global_per_min, window_sec=60):
            return JSONResponse(
                {"detail": "Too many requests. Slow down and try again."},
                status_code=429,
                headers={"Retry-After": "60"},
            )

        if method == "POST" and any(path.startswith(p) for p in _AUTH_SENSITIVE):
            if not _limiter.allow(
                f"auth:{ip}:{path}",
                limit=settings.rate_limit_auth_per_min,
                window_sec=60,
            ):
                return JSONResponse(
                    {"detail": "Too many authentication attempts. Try again in a minute."},
                    status_code=429,
                    headers={"Retry-After": "60"},
                )

        if method == "POST" and "/riu/message" in path:
            if not _limiter.allow(
                f"riu:{ip}",
                limit=settings.rate_limit_riu_per_min,
                window_sec=60,
            ):
                return JSONResponse(
                    {"detail": "Riu rate limit reached. Please wait a moment."},
                    status_code=429,
                    headers={"Retry-After": "30"},
                )

        return await call_next(request)


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        settings = get_settings()
        cl = request.headers.get("content-length")
        if cl:
            try:
                if int(cl) > settings.max_request_body_bytes:
                    return JSONResponse(
                        {"detail": "Request body too large."},
                        status_code=413,
                    )
            except ValueError:
                pass
        return await call_next(request)


_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$")


def is_valid_email(email: str) -> bool:
    email = (email or "").strip()
    if not email or len(email) > 254:
        return False
    return bool(_EMAIL_RE.match(email))


def password_policy_ok(password: str) -> tuple[bool, str]:
    """Basic password strength for production accounts."""
    if not password or len(password) < 8:
        return False, "Password must be at least 8 characters"
    if len(password) > 128:
        return False, "Password is too long"
    settings = get_settings()
    if settings.is_production:
        # require mixed class in production
        classes = sum(
            [
                any(c.islower() for c in password),
                any(c.isupper() for c in password),
                any(c.isdigit() for c in password),
                any(not c.isalnum() for c in password),
            ]
        )
        if classes < 2:
            return (
                False,
                "Password needs at least two of: lowercase, uppercase, number, symbol",
            )
        if password.lower() in {
            "password",
            "password1",
            "admin12345",
            "admin123",
            "changeme",
            "qwerty123",
        }:
            return False, "Password is too common"
    return True, ""
