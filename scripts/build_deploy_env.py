"""Build production env for Hostinger deploy (writes to data/, gitignored)."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=True)
out = ROOT / "data" / "hostinger_deploy.env"
out.parent.mkdir(exist_ok=True)

sk = os.getenv("HELIX_SECRET_KEY") or ""
if not sk or sk.startswith("dev-secret") or "change-me" in sk:
    sk = secrets.token_urlsafe(48)
pw = os.getenv("POSTGRES_PASSWORD") or secrets.token_urlsafe(24)
admin_pw = os.getenv("BOOTSTRAP_ADMIN_PASSWORD") or secrets.token_urlsafe(16)
if admin_pw in {"admin12345", "change-this-password"}:
    admin_pw = secrets.token_urlsafe(16)

lines = {
    "HELIX_ENV": "production",
    "HELIX_BASE_URL": "https://c7xai.in",
    "HELIX_SECRET_KEY": sk,
    "POSTGRES_PASSWORD": pw,
    "OPENROUTER_API_KEY": os.getenv("OPENROUTER_API_KEY", ""),
    "OPENROUTER_BASE_URL": os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
    "OPENROUTER_MODEL": os.getenv("OPENROUTER_MODEL", "x-ai/grok-4.5"),
    "OPENROUTER_SITE_URL": "https://c7xai.in",
    "OPENROUTER_SITE_NAME": "Helix",
    "APIFY_API_KEY": os.getenv("APIFY_API_KEY") or os.getenv("APIFY_TOKEN", ""),
    "APIFY_MAX_RESULTS_PER_SEARCH": os.getenv("APIFY_MAX_RESULTS_PER_SEARCH", "10"),
    "APIFY_DEDUPE_HOURS": os.getenv("APIFY_DEDUPE_HOURS", "48"),
    "APIFY_CACHE_HOURS": os.getenv("APIFY_CACHE_HOURS", "24"),
    "APIFY_FAIL_CLOSED": "true",
    "RESEND_API_KEY": os.getenv("RESEND_API_KEY", ""),
    "RESEND_FROM_EMAIL": os.getenv("RESEND_FROM_EMAIL", "Helix <onboarding@resend.dev>"),
    "BOOTSTRAP_ADMIN_EMAIL": "admin@c7xai.in",
    "BOOTSTRAP_ADMIN_PASSWORD": admin_pw,
    "ALLOW_PUBLIC_SIGNUP": "true",
    "REQUIRE_EMAIL_VERIFICATION": "false",
    "AUTO_CREATE_WORKSPACE_ON_SIGNUP": "true",
    "DEFAULT_GOLD_TARGET_COUNT": "5000",
    "DEFAULT_VARIATIONS_PER_GOLD": "4",
}
out.write_text("\n".join(f"{k}={v}" for k, v in lines.items()) + "\n", encoding="utf-8")
(ROOT / "data" / "DEPLOY_ADMIN.txt").write_text(
    f"admin email: {lines['BOOTSTRAP_ADMIN_EMAIL']}\nadmin password: {admin_pw}\n",
    encoding="utf-8",
)
print("written", out)
print("has_openrouter", bool(lines["OPENROUTER_API_KEY"]))
print("has_apify", bool(lines["APIFY_API_KEY"]))
print("has_resend", bool(lines["RESEND_API_KEY"]))
print("admin_email", lines["BOOTSTRAP_ADMIN_EMAIL"])
