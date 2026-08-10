"""Helix multi-tenant API + web console."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from helix import __version__
from helix.api.routes import agents, auth, jobs, library, riu, studio, tenants, users
from helix.api.security_middleware import (
    RateLimitMiddleware,
    RequestSizeLimitMiddleware,
    SecurityHeadersMiddleware,
)
from helix.config import get_settings
from helix.db.session import init_db
from helix.worker import start_worker

logger = logging.getLogger("helix.api")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))

_settings = get_settings()

app = FastAPI(
    title="Helix",
    description=(
        "Multi-tenant gold-data mining for LLM training — "
        "user-owned gold/synthetic libraries, synthesis, Resend auth, OpenRouter."
    ),
    version=__version__,
    docs_url="/docs" if _settings.docs_enabled else None,
    redoc_url="/redoc" if _settings.docs_enabled else None,
    openapi_url="/openapi.json" if _settings.docs_enabled else None,
)

# Middleware order: last added = outermost. Rate limit outermost for early reject.
if _settings.is_production and _settings.allowed_hosts_list:
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=_settings.allowed_hosts_list + ["testserver"],
    )

if _settings.cors_origins_list:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Accept"],
        max_age=600,
    )

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestSizeLimitMiddleware)
app.add_middleware(RateLimitMiddleware)

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(tenants.router)
app.include_router(agents.router)
app.include_router(studio.router)
app.include_router(library.router)
app.include_router(jobs.router)
app.include_router(riu.router)

static_dir = WEB_DIR / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


def _validate_production_secrets() -> None:
    s = get_settings()
    if not s.is_production:
        return
    weak_secrets = {
        "",
        "dev-secret-change-me",
        "dev-secret-change-me-in-production",
        "change-me",
        "secret",
    }
    if s.helix_secret_key in weak_secrets or len(s.helix_secret_key) < 32:
        raise RuntimeError(
            "Refusing to start: HELIX_SECRET_KEY is missing/weak in production "
            "(need ≥32 random chars)."
        )
    if s.bootstrap_admin_password in {"admin12345", "password", "admin", ""}:
        logger.warning(
            "BOOTSTRAP_ADMIN_PASSWORD looks weak — rotate it after first login."
        )


@app.on_event("startup")
def on_startup() -> None:
    _validate_production_secrets()
    init_db()
    start_worker()  # multi-batch jobs keep running after logout


@app.get("/api/health")
def health() -> dict:
    s = get_settings()
    payload: dict = {
        "status": "ok",
        "version": __version__,
        "env": s.helix_env,
    }
    # Minimal public surface by default
    if s.health_verbose or not s.is_production:
        apify_status: dict = {"configured": s.apify_configured}
        if s.apify_configured:
            try:
                from helix.services.gather.apify_client import health_check

                apify_status.update(health_check())
            except Exception as e:  # noqa: BLE001
                apify_status["ok"] = False
                apify_status["error"] = str(e)
        payload.update(
            {
                "architecture": {
                    "gather": "apify",
                    "judge": "openrouter",
                    "rule": "LLM never invents scrape data",
                },
                "llm_provider": s.llm_provider,
                "model": s.llm_model if s.llm_provider != "none" else None,
                "apify": apify_status,
            }
        )
    else:
        payload["services"] = {
            "llm": s.llm_provider != "none",
            "apify": s.apify_configured,
        }
    return payload


@app.get("/", response_class=HTMLResponse)
def home(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "index.html",
        {"version": __version__},
    )


@app.get("/app", response_class=HTMLResponse)
def console(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "app.html",
        {"version": __version__},
    )


@app.get("/docs-redirect")
def docs_redirect() -> RedirectResponse:
    if not get_settings().docs_enabled:
        return RedirectResponse("/app")
    return RedirectResponse("/docs")


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Avoid leaking stack traces in production JSON errors."""
    s = get_settings()
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    if s.is_production:
        return JSONResponse({"detail": "Internal server error"}, status_code=500)
    return JSONResponse({"detail": str(exc)}, status_code=500)


def create_app() -> FastAPI:
    return app
