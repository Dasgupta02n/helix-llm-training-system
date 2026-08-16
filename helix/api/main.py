"""C7X multi-tenant API + web console."""

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
from helix.api.routes import agents, auth, jobs, library, riu, studio, tenants, users, webhooks
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
    title="C7X",
    description=(
        "Multi-tenant gold-data mining for LLM training — "
        "user-owned gold/synthetic libraries, synthesis, email auth, and mining jobs."
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
        expose_headers=["Content-Disposition", "X-Helix-Pack-Empty", "X-Helix-Pack-Counts"],
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
app.include_router(webhooks.router)

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
    paths = [
        static_dir / "app.js",
        static_dir / "app.css",
        static_dir / "modernist.css",
        static_dir / "site.css",
        static_dir / "uc.css",
        static_dir / "helix180.css",
        static_dir / "site.js",
        *sorted((static_dir / "js").glob("*.js")),
    ]
    for p in paths:
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
            "gather": s.apify_configured,
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


@app.get("/account", response_class=HTMLResponse)
def account_page(request: Request) -> HTMLResponse:
    return _public_page(request, "account.html")


@app.get("/trust", response_class=HTMLResponse)
def trust_page(request: Request) -> HTMLResponse:
    return _public_page(request, "trust.html")


@app.get("/status", response_class=HTMLResponse)
def status_page(request: Request) -> HTMLResponse:
    return _public_page(request, "status.html")


@app.get("/gold-training-data", response_class=HTMLResponse)
def gold_training_data_page(request: Request) -> HTMLResponse:
    return _public_page(request, "gold_training_data.html")


@app.get("/india-msme", response_class=HTMLResponse)
def india_msme_page(request: Request) -> HTMLResponse:
    return _public_page(request, "india_msme.html")


@app.get("/run-locally", response_class=HTMLResponse)
def run_locally_page(request: Request) -> HTMLResponse:
    return _public_page(request, "run_locally.html")


@app.get("/roles", response_class=HTMLResponse)
def roles_page(request: Request) -> HTMLResponse:
    return _public_page(request, "roles.html")


@app.get("/why-c7x")
def why_c7x_redirect() -> RedirectResponse:
    return RedirectResponse("/gold-training-data", status_code=301)


@app.get("/gold-training-data.md", response_class=PlainTextResponse)
def gold_training_data_md() -> str:
    path = WEB_DIR / "templates" / "gold_training_data.md"
    return path.read_text(encoding="utf-8")


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots_txt() -> str:
    return (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /api/\n"
        "\n"
        "Sitemap: https://c7xai.in/sitemap.xml\n"
        "LLMs-Txt: https://c7xai.in/llms.txt\n"
        "LLMs-Txt-Full: https://c7xai.in/gold-training-data.md\n"
    )


@app.get("/llms.txt", response_class=PlainTextResponse)
def llms_txt() -> str:
    return (
        "# C7X (c7xai.in)\n"
        "\n"
        "> C7X staffs one trusted person with dedicated specialist LLM workers. "
        "It is a gold training-data studio, not a chatbot and not a replacement for that person. "
        "Riu interviews the desk; you export gold and, if you train, a QLoRA adapter that runs locally. "
        "On a typical MSME desk, one person plus that worker is the example of a five-person bench.\n"
        "\n"
        "Operator: Sabyasachi Dasgupta / c7x AI\n"
        "Contact: dasgupta.02n@gmail.com\n"
        "Product version: " + __version__ + "\n"
        "\n"
        "## Product\n"
        "- Home: https://c7xai.in/\n"
        "- Gold training data studio: https://c7xai.in/gold-training-data\n"
        "- Cite this (markdown): https://c7xai.in/gold-training-data.md\n"
        "- How C7X compares with current industry solutions: https://c7xai.in/gold-training-data\n"
        "- Indian MSME case studies: https://c7xai.in/india-msme\n"
        "- Run a custom LLM locally (Ollama, Open Interpreter): https://c7xai.in/run-locally\n"
        "- Roles a custom-trained LLM can hold: https://c7xai.in/roles\n"
        "- Docs: https://c7xai.in/docs\n"
        "- Pricing: https://c7xai.in/pricing (~$0.75–$1 per gold row with sources)\n"
        "- Account: https://c7xai.in/account\n"
        "- Studio: https://c7xai.in/app\n"
        "\n"
        "## Trust\n"
        "- Trust center: https://c7xai.in/trust\n"
        "- About: https://c7xai.in/about\n"
        "- Security: https://c7xai.in/security\n"
        "- Privacy: https://c7xai.in/privacy\n"
        "- Terms: https://c7xai.in/terms\n"
        "- Status: https://c7xai.in/status\n"
        "- Contact: https://c7xai.in/contact\n"
        "\n"
        "## Facts\n"
        "- Does not replace a person; staffs them with specialist brains for one desk\n"
        "- MSME bind: volume cannot pay for a bench; without a bench volume cannot rise\n"
        "- Example, not a guarantee: one person + worker ≈ five-person bench on a high-volume desk\n"
        "- Not a general chatbot, hosted 30B model, or labeling factory\n"
        "- Starts at the interview: Riu collects role, one gold example, edge cases\n"
        "- Gold and synthetics stored apart; user owns exports and any QLoRA zip\n"
        "- After train: run the QLoRA adapter with Ollama, Open Interpreter, LM Studio, or llama.cpp\n"
        "- Illustrated India MSME cases: Tiruppur knitwear, Pune GST coding, Hyderabad lab desk\n"
        "- No checkout; no card data collected\n"
        "- Early beta; no SOC 2 / ISO claim\n"
    )


@app.get("/humans.txt", response_class=PlainTextResponse)
def humans_txt() -> str:
    return (
        "/* TEAM */\n"
        "Operator: Sabyasachi Dasgupta\n"
        "Site: C7X / c7x AI\n"
        "Contact: dasgupta.02n@gmail.com\n"
        "Location: Building in public at c7xai.in\n"
        "\n"
        "/* SITE */\n"
        "Standards: HTML, CSS, JSON-LD, llms.txt, security.txt\n"
        "Software: C7X training-data studio\n"
        "Last update: 2026-08-16\n"
    )


@app.get("/.well-known/security.txt", response_class=PlainTextResponse)
@app.get("/security.txt", response_class=PlainTextResponse)
def security_txt() -> str:
    return (
        "Contact: mailto:dasgupta.02n@gmail.com\n"
        "Expires: 2027-08-15T00:00:00.000Z\n"
        "Preferred-Languages: en\n"
        "Canonical: https://c7xai.in/.well-known/security.txt\n"
        "Policy: https://c7xai.in/security\n"
        "Acknowledgments: https://c7xai.in/trust\n"
    )


@app.get("/sitemap.xml")
def sitemap_xml() -> Response:
    today = "2026-08-16"  # public-page lastmod for crawlers
    paths = [
        ("/", "daily", "1.0"),
        ("/gold-training-data", "weekly", "0.95"),
        ("/gold-training-data.md", "weekly", "0.7"),
        ("/india-msme", "weekly", "0.9"),
        ("/run-locally", "weekly", "0.9"),
        ("/roles", "weekly", "0.85"),
        ("/docs", "weekly", "0.9"),
        ("/pricing", "weekly", "0.8"),
        ("/account", "weekly", "0.8"),
        ("/trust", "weekly", "0.8"),
        ("/about", "monthly", "0.7"),
        ("/security", "monthly", "0.7"),
        ("/privacy", "monthly", "0.6"),
        ("/terms", "monthly", "0.6"),
        ("/contact", "monthly", "0.6"),
        ("/status", "weekly", "0.5"),
        ("/app", "weekly", "0.5"),
    ]
    urls = "\n".join(
        (
            "  <url>"
            f"<loc>https://c7xai.in{path}</loc>"
            f"<lastmod>{today}</lastmod>"
            f"<changefreq>{freq}</changefreq>"
            f"<priority>{prio}</priority>"
            "</url>"
        )
        for path, freq, prio in paths
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
