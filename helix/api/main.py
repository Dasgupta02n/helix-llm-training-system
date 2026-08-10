"""Helix multi-tenant API + web console."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from helix import __version__
from helix.api.routes import agents, auth, jobs, library, studio, tenants, users
from helix.config import get_settings
from helix.db.session import init_db
from helix.worker import start_worker

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))

app = FastAPI(
    title="Helix",
    description=(
        "Multi-tenant gold-data mining for LLM training — "
        "user-owned gold/synthetic libraries, synthesis, Resend auth, OpenRouter."
    ),
    version=__version__,
)

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(tenants.router)
app.include_router(agents.router)
app.include_router(studio.router)
app.include_router(library.router)
app.include_router(jobs.router)

static_dir = WEB_DIR / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.on_event("startup")
def on_startup() -> None:
    init_db()
    start_worker()  # multi-batch jobs keep running after logout


@app.get("/api/health")
def health() -> dict:
    s = get_settings()
    apify_status = {"configured": s.apify_configured}
    if s.apify_configured:
        try:
            from helix.services.gather.apify_client import health_check

            apify_status.update(health_check())
        except Exception as e:  # noqa: BLE001
            apify_status["ok"] = False
            apify_status["error"] = str(e)
    return {
        "status": "ok",
        "version": __version__,
        "env": s.helix_env,
        "architecture": {
            "gather": "apify",
            "judge": "openrouter",
            "rule": "LLM never invents scrape data",
        },
        "llm_provider": s.llm_provider,
        "model": s.llm_model if s.llm_provider != "none" else None,
        "apify": apify_status,
    }


@app.get("/", response_class=HTMLResponse)
def home(request: Request) -> HTMLResponse:
    # Starlette 1.x: TemplateResponse(request, name, context)
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
    return RedirectResponse("/docs")


def create_app() -> FastAPI:
    return app
