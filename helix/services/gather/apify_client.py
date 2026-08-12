"""Apify HTTP client — the only allowed external gatherer."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from helix.config import get_settings

logger = logging.getLogger("helix.apify")

# Actor IDs use ~ in API path (user~name)
ACTORS: dict[str, str] = {
    # Cross-source web search → cheap discovery of URLs/snippets
    "search": "apify/google-search-scraper",
    "blog": "apify/website-content-crawler",
    "instagram": "apify/instagram-scraper",
    "tiktok": "clockworks/tiktok-scraper",
    "youtube": "streamers/youtube-scraper",
    "x": "apidojo/tweet-scraper",
    # Single-page evidence fetch
    "page": "apify/cheerio-scraper",
}


def _token() -> str:
    return get_settings().apify_key


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token()}"}


def actor_path(actor_id: str) -> str:
    return actor_id.replace("/", "~")


def run_actor(
    actor_id: str,
    run_input: dict[str, Any],
    *,
    timeout_secs: int | None = None,
    memory_mbytes: int = 1024,
) -> dict[str, Any]:
    """Start actor and wait for finish. Returns run object with defaultDatasetId."""
    settings = get_settings()
    if not settings.apify_configured:
        raise RuntimeError("APIFY_API_KEY is not configured")

    timeout = timeout_secs or settings.apify_default_timeout_secs
    base = settings.apify_base_url.rstrip("/")
    path = actor_path(actor_id)
    url = f"{base}/acts/{path}/runs"
    params = {
        "waitForFinish": min(timeout, 300),
        "timeout": timeout,
        "memory": memory_mbytes,
    }
    with httpx.Client(timeout=timeout + 30.0) as client:
        resp = client.post(
            url,
            params=params,
            headers=_headers(),
            json=run_input,
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"Apify run failed ({resp.status_code}): {resp.text[:500]}")
        data = resp.json()
        # API wraps as { data: { ...run } }
        run = data.get("data") or data
        status = (run.get("status") or "").upper()
        if status == "SUCCEEDED":
            return run
        if status in {"FAILED", "TIMED-OUT", "ABORTED", "TIMED_OUT"}:
            raise RuntimeError(
                f"Apify actor {actor_id} ended with status={status}: "
                f"{run.get('statusMessage') or ''}"
            )
        # Still running / ready — wait for finish
        run_id = run.get("id")
        if not run_id:
            raise RuntimeError(f"Apify actor {actor_id} returned no run id (status={status})")
        return wait_for_run(run_id, timeout_secs=timeout)


def wait_for_run(run_id: str, timeout_secs: int = 120) -> dict[str, Any]:
    settings = get_settings()
    base = settings.apify_base_url.rstrip("/")
    url = f"{base}/actor-runs/{run_id}"
    with httpx.Client(timeout=timeout_secs + 30.0) as client:
        resp = client.get(
            url,
            params={"waitForFinish": min(timeout_secs, 300)},
            headers=_headers(),
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"Apify wait failed: {resp.text[:400]}")
        data = resp.json()
        run = data.get("data") or data
        if run.get("status") != "SUCCEEDED":
            raise RuntimeError(
                f"Apify run {run_id} status={run.get('status')}: {run.get('statusMessage')}"
            )
        return run


def fetch_dataset_items(dataset_id: str, limit: int = 50) -> list[dict[str, Any]]:
    settings = get_settings()
    base = settings.apify_base_url.rstrip("/")
    url = f"{base}/datasets/{dataset_id}/items"
    with httpx.Client(timeout=60.0) as client:
        resp = client.get(
            url,
            params={"format": "json", "clean": 1, "limit": limit},
            headers=_headers(),
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"Apify dataset fetch failed: {resp.text[:400]}")
        data = resp.json()
        if isinstance(data, list):
            return data
        return data.get("items") or data.get("data") or []


def search_web(
    query: str,
    max_results: int = 10,
    *,
    max_pages: int = 1,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Batch web search via Google Search scraper — primary discovery gatherer."""
    actor = ACTORS["search"]
    pages = max(1, min(int(max_pages or 1), 3))
    per_page = min(max(max_results, 5), 10)
    run_input = {
        "queries": query,
        "maxPagesPerQuery": pages,
        "resultsPerPage": per_page,
        "mobileResults": False,
        "languageCode": "en",
        "forceExactMatch": False,
        "includeUnfilteredResults": False,
    }
    run = run_actor(actor, run_input, memory_mbytes=2048)
    dataset_id = run.get("defaultDatasetId")
    fetch_limit = max(max_results, per_page * pages)
    items = fetch_dataset_items(dataset_id, limit=fetch_limit) if dataset_id else []
    return items, {
        "run_id": run.get("id"),
        "dataset_id": dataset_id,
        "actor": actor,
        "max_pages": pages,
    }


def fetch_page(url: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """Single-URL evidence fetch — not done by the LLM."""
    actor = ACTORS["page"]
    run_input = {
        "startUrls": [{"url": url}],
        "maxRequestsPerCrawl": 1,
        "pageFunction": """async function pageFunction(context) {
            const { $, request } = context;
            const title = $('title').first().text() || '';
            const text = $('body').text().replace(/\\s+/g, ' ').trim().slice(0, 12000);
            return { url: request.url, title, text };
        }""",
    }
    run = run_actor(actor, run_input, timeout_secs=90, memory_mbytes=1024)
    dataset_id = run.get("defaultDatasetId")
    items = fetch_dataset_items(dataset_id, limit=5) if dataset_id else []
    item = items[0] if items else {"url": url, "title": "", "text": ""}
    return item, {"run_id": run.get("id"), "dataset_id": dataset_id, "actor": actor}


def health_check() -> dict[str, Any]:
    settings = get_settings()
    if not settings.apify_configured:
        return {"ok": False, "error": "APIFY_API_KEY not set"}
    base = settings.apify_base_url.rstrip("/")
    try:
        with httpx.Client(timeout=20.0) as client:
            resp = client.get(f"{base}/users/me", headers=_headers())
            if resp.status_code >= 400:
                return {"ok": False, "error": resp.text[:200]}
            data = resp.json().get("data") or resp.json()
            return {
                "ok": True,
                "username": data.get("username"),
                "plan": (data.get("plan") or {}).get("id") if isinstance(data.get("plan"), dict) else data.get("plan"),
            }
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
