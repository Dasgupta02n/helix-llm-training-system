"""Helix multi-tenant API + web console."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
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

# Product docs live at /docs (public HTML). OpenAPI UI is separate when enabled.
app = FastAPI(
    title="Helix",
    description=(
        "Multi-tenant gold-data mining for LLM training — "
        "user-owned gold/synthetic libraries, synthesis, Resend auth, OpenRouter."
    ),
    version=__version__,
    docs_url="/api/docs" if _settings.docs_enabled else None,
    redoc_url="/api/redoc" if _settings.docs_enabled else None,
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


class _NoCacheStaticFiles(StaticFiles):
    """Serve static assets with revalidate headers so redeploys don't stick behind cache."""

    async def get_response(self, path: str, scope):  # type: ignore[no-untyped-def]
        response = await super().get_response(path, scope)
        # JS/CSS must revalidate; hashed query strings also used in templates
        if path.endswith((".js", ".css", ".html")):
            response.headers["Cache-Control"] = "no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
        else:
            response.headers.setdefault("Cache-Control", "public, max-age=3600")
        return response


app.mount("/static", _NoCacheStaticFiles(directory=str(static_dir)), name="static")


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


def _static_asset_version() -> str:
    """Content-ish stamp for cache busting static URLs in templates."""
    import hashlib

    h = hashlib.sha1()
    for name in ("app.js", "app.css", "modernist.css"):
        p = static_dir / name
        if p.exists():
            h.update(p.read_bytes()[:200_000])
            h.update(str(p.stat().st_mtime_ns).encode())
    return h.hexdigest()[:12]


@app.on_event("startup")
def on_startup() -> None:
    _validate_production_secrets()
    init_db()
    start_worker()  # multi-batch jobs keep running after logout
    # Warm asset version for templates
    app.state.static_v = _static_asset_version()
    # One-shot: reject gold poisoned by cross-plan corpus contamination
    try:
        from helix.db.session import SessionLocal
        from helix.services.gold_quality import backfill_reject_cross_domain_gold

        db = SessionLocal()
        try:
            result = backfill_reject_cross_domain_gold(db)
            if result.get("newly_rejected") or result.get("already_rejected_annotated"):
                logger.info(
                    "cross_domain_gold_backfill scanned=%s newly_rejected=%s annotated=%s",
                    result.get("scanned"),
                    result.get("newly_rejected"),
                    result.get("already_rejected_annotated"),
                )
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        logger.exception("cross_domain_gold_backfill failed (non-fatal)")


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


def _template_ctx(**extra: object) -> dict:
    return {
        "version": __version__,
        "static_v": getattr(app.state, "static_v", None) or _static_asset_version(),
        **extra,
    }


@app.get("/", response_class=HTMLResponse)
def home(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "index.html",
        _template_ctx(),
    )


@app.get("/app", response_class=HTMLResponse)
def console(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "app.html",
        _template_ctx(),
    )


def _public_page(request: Request, name: str) -> HTMLResponse:
    return templates.TemplateResponse(request, name, _template_ctx())


@app.get("/privacy", response_class=HTMLResponse)
def privacy_page(request: Request) -> HTMLResponse:
    return _public_page(request, "privacy.html")


@app.get("/terms", response_class=HTMLResponse)
def terms_page(request: Request) -> HTMLResponse:
    return _public_page(request, "terms.html")


@app.get("/pricing", response_class=HTMLResponse)
def pricing_page(request: Request) -> HTMLResponse:
    return _public_page(request, "pricing.html")


@app.get("/docs", response_class=HTMLResponse)
def product_docs_page(request: Request) -> HTMLResponse:
    """Product documentation (always public). OpenAPI stays behind enable_api_docs."""
    return _public_page(request, "docs.html")


@app.get("/about", response_class=HTMLResponse)
def about_page(request: Request) -> HTMLResponse:
    return _public_page(request, "about.html")


@app.get("/contact", response_class=HTMLResponse)
def contact_page(request: Request) -> HTMLResponse:
    return _public_page(request, "contact.html")


@app.get("/security", response_class=HTMLResponse)
def security_page(request: Request) -> HTMLResponse:
    return _public_page(request, "security.html")


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots_txt() -> str:
    return (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /api/\n"
        "\n"
        "Sitemap: https://c7xai.in/sitemap.xml\n"
    )


@app.get("/sitemap.xml")
def sitemap_xml() -> Response:
    paths = [
        "/",
        "/app",
        "/docs",
        "/pricing",
        "/about",
        "/contact",
        "/privacy",
        "/terms",
        "/security",
    ]
    urls = "\n".join(
        f"  <url><loc>https://c7xai.in{p}</loc><changefreq>weekly</changefreq></url>"
        for p in paths
    )
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{urls}\n"
        "</urlset>\n"
    )
    return Response(content=body, media_type="application/xml")


@app.get("/docs-redirect")
def docs_redirect() -> RedirectResponse:
    # Always land on product docs; OpenAPI is separate when enabled
    return RedirectResponse("/docs", status_code=307)


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
