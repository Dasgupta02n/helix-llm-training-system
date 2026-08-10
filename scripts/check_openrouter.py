"""Check OpenRouter key presence and live API connectivity (never prints full secrets)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"


def mask(value: str) -> str:
    if not value:
        return "(empty)"
    if len(value) <= 12:
        return f"(len={len(value)})"
    return f"{value[:10]}…{value[-4:]} (len={len(value)})"


def main() -> int:
    print(f".env path: {ENV_PATH}")
    print(f".env exists: {ENV_PATH.exists()}")

    if ENV_PATH.exists():
        raw = ENV_PATH.read_text(encoding="utf-8-sig")
        print(f".env bytes: {len(raw.encode('utf-8'))}")
        for i, line in enumerate(raw.splitlines(), 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if "=" not in stripped:
                print(f"  line {i}: invalid (no =)")
                continue
            key, val = stripped.split("=", 1)
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            sensitive = any(s in key.upper() for s in ("KEY", "SECRET", "PASSWORD", "TOKEN"))
            if sensitive:
                print(f"  line {i}: {key} = {mask(val)}")
            else:
                print(f"  line {i}: {key} = {val}")

    load_dotenv(ENV_PATH, override=True)
    api_key = (os.getenv("OPENROUTER_API_KEY") or "").strip()
    base = (os.getenv("OPENROUTER_BASE_URL") or "https://openrouter.ai/api/v1").rstrip("/")
    model = os.getenv("OPENROUTER_MODEL") or "x-ai/grok-4.5"
    site = os.getenv("OPENROUTER_SITE_URL") or os.getenv("HELIX_BASE_URL") or "http://localhost:8000"
    title = os.getenv("OPENROUTER_SITE_NAME") or "Helix"

    print()
    print(f"Loaded OPENROUTER_API_KEY: {mask(api_key)}")
    print(f"Base URL: {base}")
    print(f"Model: {model}")

    if not api_key:
        print("\nRESULT: FAIL — OPENROUTER_API_KEY is missing or empty in .env")
        print("Add a line like: OPENROUTER_API_KEY=sk-or-v1-...")
        print("No spaces around =, no quotes needed, not commented with #")
        return 1

    if api_key.startswith("sk-or-v1-your") or "your-key" in api_key.lower():
        print("\nRESULT: FAIL — placeholder key detected, not a real key")
        return 1

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": site,
        "X-Title": title,
    }

    # 1) Auth / models list
    print("\n[1/2] GET /models (auth check)…")
    try:
        r = httpx.get(f"{base}/models", headers=headers, timeout=30.0)
        print(f"  status: {r.status_code}")
        if r.status_code != 200:
            print(f"  body: {r.text[:400]}")
            print("\nRESULT: FAIL — OpenRouter rejected the key (or endpoint)")
            return 1
        data = r.json()
        models = data.get("data") or []
        print(f"  models visible: {len(models)}")
        ids = {m.get("id") for m in models if isinstance(m, dict)}
        print(f"  configured model present: {model in ids}")
        if model not in ids:
            # still may work for some gated models
            close = [m for m in sorted(ids) if m and ("grok" in m or "x-ai" in m)][:8]
            print(f"  sample x-ai/grok models: {close}")
    except Exception as e:
        print(f"  error: {e}")
        print("\nRESULT: FAIL — network/API error")
        return 1

    # 2) Tiny chat completion
    print("\n[2/2] POST /chat/completions (live generation)…")
    payload = {
        "model": model,
        "messages": [
            {"role": "user", "content": "Reply with exactly: HELIX_OK"},
        ],
        "max_tokens": 20,
        "temperature": 0,
    }
    try:
        r = httpx.post(
            f"{base}/chat/completions",
            headers=headers,
            content=json.dumps(payload),
            timeout=60.0,
        )
        print(f"  status: {r.status_code}")
        if r.status_code != 200:
            print(f"  body: {r.text[:600]}")
            print("\nRESULT: FAIL — chat completion failed (key ok, but model/request issue)")
            return 2
        body = r.json()
        content = (
            body.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
        )
        usage = body.get("usage") or {}
        print(f"  reply: {content!r}")
        print(f"  usage: {usage}")
        print("\nRESULT: OK — OpenRouter API key is working")
        return 0
    except Exception as e:
        print(f"  error: {e}")
        print("\nRESULT: FAIL — chat request error")
        return 1


if __name__ == "__main__":
    sys.exit(main())
