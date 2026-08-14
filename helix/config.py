"""Helix configuration — local SQLite or VPS multi-tenant with OpenRouter."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    helix_env: str = "local"
    helix_secret_key: str = "dev-secret-change-me-in-production"
    helix_base_url: str = "http://localhost:8000"

    database_url: str = f"sqlite:///{DATA_DIR / 'helix.db'}"

    # OpenRouter is the production power source
    openrouter_api_key: str = ""
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openrouter_model: str = "x-ai/grok-4.5"
    openrouter_site_url: str = "http://localhost:8000"
    openrouter_site_name: str = "Helix"

    # Optional local fallback (direct xAI)
    xai_api_key: str = ""
    xai_base_url: str = "https://api.x.ai/v1"
    xai_model: str = "grok-4.5"

    # Apify — ALL gathering (search/scrape). OpenRouter never gathers.
    apify_api_key: str = ""  # also accepts APIFY_TOKEN via alias below
    apify_token: str = ""  # optional alternate env name
    apify_base_url: str = "https://api.apify.com/v2"
    apify_default_timeout_secs: int = 120
    apify_max_results_per_search: int = 10
    apify_dedupe_hours: int = 48
    apify_cache_hours: int = 24
    # If true and Apify fails hard, do not invent data — return error
    apify_fail_closed: bool = True

    bootstrap_admin_email: str = "admin@example.com"
    bootstrap_admin_password: str = "admin12345"

    # Resend (transactional email)
    resend_api_key: str = ""
    resend_from_email: str = "Helix <onboarding@resend.dev>"
    resend_reply_to: str = ""

    # Auth policy
    require_email_verification: bool = True
    # After email verify, admin must click Approve in email before login
    require_admin_approval: bool = True
    admin_notification_email: str = "dasgupta.02n@gmail.com"
    auth_token_expire_hours: int = 24
    # Approval links can live longer so admin has time to act
    admin_approve_token_expire_hours: int = 72
    allow_public_signup: bool = True
    auto_create_workspace_on_signup: bool = True

    max_tool_rounds: int = 12
    default_tenant_monthly_budget_usd: float = 50.0
    # Local default 7d; production should set ACCESS_TOKEN_EXPIRE_MINUTES lower (e.g. 1440)
    access_token_expire_minutes: int = 60 * 24 * 7  # 7 days

    # User library defaults (indefinite storage in user account)
    default_gold_target_count: int = 5000
    default_variations_per_gold: int = 4
    # max per synthesis API call to avoid runaway cost
    max_synthesis_batch_golds: int = 50
    max_variations_per_gold: int = 20

    relevance_threshold: float = 0.55
    verification_verify_threshold: float = 0.75
    verification_reject_threshold: float = 0.4
    match_high: float = 0.85
    match_low: float = 0.4
    promotion_regression_tolerance: float = 0.03

    # ── Security ───────────────────────────────────────────────────
    rate_limit_enabled: bool = True
    rate_limit_global_per_min: int = 180
    rate_limit_auth_per_min: int = 20
    rate_limit_riu_per_min: int = 30
    max_request_body_bytes: int = 1_048_576  # 1 MiB
    trust_proxy_headers: bool = True  # Caddy / reverse proxy sets X-Forwarded-For
    # Comma-separated hostnames allowed in production (empty = skip TrustedHost)
    allowed_hosts: str = (
        "c7xai.in,www.c7xai.in,localhost,127.0.0.1,187.127.156.152"
    )
    # Disable /docs and /openapi.json in production unless explicitly enabled
    enable_api_docs: bool = False
    # Public CORS origins (comma-separated). Empty = no CORS middleware (same-origin only).
    cors_origins: str = ""
    # Expose detailed health (Apify username/plan) only when true
    health_verbose: bool = False

    # Double Helix trains only on RunPod Serverless (never GPU Cloud pods).
    runpod_api_key: str = ""
    runpod_serverless_endpoint_id: str = ""
    hf_token: str = ""

    @property
    def is_production(self) -> bool:
        return self.helix_env.lower() in {"production", "prod", "vps"}

    @property
    def allowed_hosts_list(self) -> list[str]:
        return [h.strip() for h in (self.allowed_hosts or "").split(",") if h.strip()]

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in (self.cors_origins or "").split(",") if o.strip()]

    @property
    def docs_enabled(self) -> bool:
        if self.enable_api_docs:
            return True
        return not self.is_production

    @property
    def llm_provider(self) -> str:
        if self.openrouter_api_key:
            return "openrouter"
        if self.xai_api_key:
            return "xai"
        return "none"

    @property
    def llm_api_key(self) -> str:
        if self.llm_provider == "openrouter":
            return self.openrouter_api_key
        return self.xai_api_key

    @property
    def llm_base_url(self) -> str:
        if self.llm_provider == "openrouter":
            return self.openrouter_base_url
        return self.xai_base_url

    @property
    def llm_model(self) -> str:
        if self.llm_provider == "openrouter":
            return self.openrouter_model
        return self.xai_model

    @property
    def apify_key(self) -> str:
        return (self.apify_api_key or self.apify_token or "").strip()

    @property
    def apify_configured(self) -> bool:
        return bool(self.apify_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()


CATEGORIES = [
    "beauty",
    "fitness",
    "tech",
    "food",
    "fashion",
    "gaming",
    "travel",
    "finance",
]

# Default gather channels when a plan does not name sources.
# Free-text plan sources (education sites, forums, docs, …) are adapted in
# helix.services.source_adapter — do not treat this list as the only reachable set.
SOURCES = ["web", "blog", "docs", "forum", "youtube", "instagram", "tiktok", "x"]
