"""Smart gather: batch Apify calls, cache, dedupe, code relevance — never LLM."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from helix.config import get_settings
from helix.db import models as m
from helix.services.cost_tracking import record_apify_spend
from helix.services.gather import apify_client


def _uid(prefix: str = "gth_") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(*parts: str) -> str:
    blob = "|".join(p or "" for p in parts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def code_relevance(
    title: str,
    snippet: str,
    category: str,
    query: str = "",
    *,
    url: str = "",
    domain_kind: str = "",
) -> dict[str, Any]:
    """Cheap deterministic relevance — runs BEFORE any OpenRouter judgment."""
    # Domain-aware scoring (support demotes ads, boosts FAQ/help)
    if domain_kind:
        try:
            from helix.services.research_targets import score_item_for_kind

            kind_score = score_item_for_kind(
                kind=domain_kind,
                title=title or "",
                snippet=snippet or "",
                url=url or "",
                category=category or "",
                query=query or "",
            )
            score = float(kind_score["relevance_score"])
        except Exception:  # noqa: BLE001
            domain_kind = ""
            score = 0.3
    if not domain_kind:
        text = f"{title} {snippet} {query}".lower()
        cat = (category or "").lower()
        base = 0.3
        if cat and cat in text:
            base += 0.28
        words = {w for w in re.findall(r"[a-z0-9]{3,}", text)}
        cwords = {w for w in re.findall(r"[a-z0-9]{3,}", cat)}
        if cwords:
            base += 0.25 * (len(words & cwords) / max(len(cwords), 1))
        for w in ("sponsored", "partner", "collab", "campaign", "#ad", "review", "launch", "promo"):
            if w in text:
                base += 0.08
                break
        if query:
            qwords = {w for w in re.findall(r"[a-z0-9]{3,}", query.lower())}
            if qwords:
                base += 0.15 * (len(words & qwords) / max(len(qwords), 1))
        score = round(min(base, 0.99), 3)
    else:
        score = round(min(score, 0.99), 3)

    threshold = get_settings().relevance_threshold
    # Support domains: slightly lower threshold so FAQ hits survive, ads already demoted
    if domain_kind == "support":
        threshold = max(0.42, threshold - 0.08)
    needs_judgment = score >= threshold
    return {
        "relevance_score": score,
        "above_threshold": score >= threshold,
        "threshold": threshold,
        "needs_judgment": needs_judgment,
        "scored_by": "code",
        "domain_kind": domain_kind or None,
    }


def _normalize_search_item(raw: dict[str, Any], source: str, category: str) -> dict[str, Any]:
    """Map Apify google-search (or similar) rows to a flat gather item."""
    title = (
        raw.get("title")
        or raw.get("name")
        or raw.get("organicResults", [{}])[0].get("title")
        if isinstance(raw.get("organicResults"), list) and raw.get("organicResults")
        else ""
    ) or ""
    url = raw.get("url") or raw.get("link") or raw.get("displayedUrl") or ""
    snippet = (
        raw.get("description")
        or raw.get("snippet")
        or raw.get("text")
        or ""
    )
    # google-search-scraper often nests organic results — flatten handled by caller
    if not title and isinstance(raw.get("title"), str):
        title = raw["title"]
    creator = raw.get("author") or raw.get("creator") or raw.get("channelName")
    brand = raw.get("brand")
    if not brand and title:
        # light heuristic only for storage, not LLM
        brand = title.split("-")[0].strip()[:80] if "-" in title else None
    content_hash = _hash(url or title, snippet[:200], source)
    return {
        "source": source or "web",
        "category": category,
        "external_id": raw.get("id") or url or content_hash[:16],
        "url": url,
        "title": (title or "Untitled")[:500],
        "brand": brand,
        "creator": creator,
        "snippet": (snippet or "")[:2000],
        "content_text": (snippet or "")[:8000],
        "raw": raw,
        "content_hash": content_hash,
    }


def _flatten_google_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flat: list[dict[str, Any]] = []
    for it in items:
        organic = it.get("organicResults")
        if isinstance(organic, list) and organic:
            for row in organic:
                flat.append(row)
        else:
            flat.append(it)
    return flat


def _cache_get(db: Session, tenant_id: str, cache_key: str) -> m.GatherCache | None:
    row = (
        db.query(m.GatherCache)
        .filter_by(tenant_id=tenant_id, cache_key=cache_key)
        .first()
    )
    if not row:
        return None
    exp = row.expires_at
    if exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if exp < _now():
        return None
    return row


def _recent_search_dup(db: Session, tenant_id: str, source: str, query: str) -> bool:
    settings = get_settings()
    cutoff = _now() - timedelta(hours=settings.apify_dedupe_hours)
    rows = (
        db.query(m.RecentSearch)
        .filter_by(tenant_id=tenant_id, source=source)
        .order_by(m.RecentSearch.created_at.desc())
        .limit(100)
        .all()
    )
    ql = query.lower().strip()
    for r in rows:
        created = r.created_at
        if created and created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created and created < cutoff:
            continue
        if (r.query or "").lower().strip() == ql:
            return True
    return False


def _as_query_list(query: str | list[str] | tuple[str, ...] | None) -> list[str]:
    if query is None:
        return []
    if isinstance(query, (list, tuple)):
        return [str(q).strip() for q in query if str(q).strip()]
    parts = [p.strip() for p in str(query).splitlines() if p.strip()]
    return parts or ([str(query).strip()] if str(query).strip() else [])


def gather_search(
    db: Session,
    *,
    tenant_id: str,
    category: str,
    source: str,
    query: str | list[str] | tuple[str, ...],
    max_results: int | None = None,
    force_refresh: bool = False,
    deep: bool = False,
    domain_kind: str = "",
) -> dict[str, Any]:
    """
    Smart Apify search:
    - batch size capped (higher when deep=True for thin-yield expansion)
    - cache hit within apify_cache_hours (skipped when force_refresh)
    - query dedupe within apify_dedupe_hours (skipped when force_refresh)
    - domain-aware code relevance; only needs_judgment items proceed toward OpenRouter
    """
    settings = get_settings()
    cap = 20 if deep else 10
    default_max = settings.apify_max_results_per_search
    if deep:
        default_max = max(default_max, 15)
    max_results = max(1, min(int(max_results or default_max), cap))
    source = (source or "blog").lower()
    raw_queries = _as_query_list(query) or [str(category or "training").strip()]
    from helix.services.source_adapter import SOCIAL_CHANNELS, adapt_source

    spec = adapt_source(source)
    gather_channel = spec.get("channel") or "web"
    enriched: list[str] = []
    for one in raw_queries:
        search_q = one.strip()
        if category and category.lower() not in search_q.lower():
            search_q = f"{search_q} {category}".strip()
        if gather_channel in SOCIAL_CHANNELS:
            hint = _site_hint(gather_channel)
            if hint and f"site:{hint}" not in search_q.lower():
                search_q = f"{search_q} site:{hint}"
        elif spec.get("operators"):
            op0 = spec["operators"][0]
            if op0 and op0.lower() not in search_q.lower():
                search_q = f"{search_q} {op0}".strip()
        if search_q:
            enriched.append(search_q)
    search_queries = enriched or raw_queries
    search_q = " | ".join(search_queries)
    source = gather_channel if gather_channel != "unreachable" else "web"

    cache_key = _hash(tenant_id, source, search_q, str(max_results), domain_kind or "")
    job = m.GatherJob(
        id=_uid("gjob_"),
        tenant_id=tenant_id,
        source=source,
        category=category or "",
        query=search_q,
        status="running",
    )
    db.add(job)
    db.commit()

    if not force_refresh and _recent_search_dup(db, tenant_id, source, search_q):
        job.status = "cached"
        job.from_cache = True
        job.error = "Skipped gather — same query within dedupe window"
        job.finished_at = _now()
        # Return existing gather items for this query if any
        existing = (
            db.query(m.GatherItem)
            .filter_by(tenant_id=tenant_id, category=category or "", source=source)
            .filter(m.GatherItem.needs_judgment.is_(True))
            .order_by(m.GatherItem.created_at.desc())
            .limit(max_results)
            .all()
        )
        job.item_count = len(existing)
        job.needs_judgment_count = len(existing)
        db.commit()
        return {
            "job_id": job.id,
            "from_cache": True,
            "deduped_query": True,
            "results": [_item_public(i) for i in existing],
            "needs_judgment": [_item_public(i) for i in existing],
            "discarded_low_relevance": 0,
            "gatherer": "apify",
            "message": "Query recently run — reused stored items (no new gather spend).",
        }

    cached = None if force_refresh else _cache_get(db, tenant_id, cache_key)
    raw_items: list[dict[str, Any]] = []
    meta: dict[str, Any] = {}

    if cached:
        job.from_cache = True
        job.status = "cached"
        try:
            raw_items = json.loads(cached.payload_json)
        except json.JSONDecodeError:
            raw_items = []
        meta = {"from_cache": True}
    else:
        if not settings.apify_configured:
            job.status = "error"
            job.error = "APIFY_API_KEY not configured"
            job.finished_at = _now()
            db.commit()
            raise RuntimeError("Gathering is not configured on this server.")
        try:
            pages = 2 if deep else 1
            take = max_results * max(1, len(search_queries))
            raw_items, meta = apify_client.search_web(
                search_queries, max_results=max_results, max_pages=pages
            )
            raw_items = _flatten_google_items(raw_items)[:take]
            # store cache
            db.add(
                m.GatherCache(
                    id=_uid("gc_"),
                    tenant_id=tenant_id,
                    cache_key=cache_key,
                    source=source,
                    query=search_q,
                    category=category or "",
                    payload_json=json.dumps(raw_items),
                    item_count=len(raw_items),
                    expires_at=_now() + timedelta(hours=settings.apify_cache_hours),
                )
            )
            job.apify_run_id = meta.get("run_id")
            job.apify_dataset_id = meta.get("dataset_id")
            cost_usd = float(meta.get("cost_usd") or 0.0)
            job.cost_usd = cost_usd
            if cost_usd > 0:
                tenant = db.query(m.Tenant).filter_by(id=tenant_id).first()
                record_apify_spend(tenant, cost_usd)
        except Exception as e:  # noqa: BLE001
            job.status = "error"
            job.error = str(e)[:1000]
            job.finished_at = _now()
            db.commit()
            if settings.apify_fail_closed:
                raise
            return {
                "job_id": job.id,
                "error": str(e),
                "results": [],
                "needs_judgment": [],
                "gatherer": "apify",
                "apify_cost_usd": 0.0,
            }

    results_out: list[dict[str, Any]] = []
    needs_j: list[dict[str, Any]] = []
    discarded = 0

    for raw in raw_items:
        norm = _normalize_search_item(raw, source, category)
        # URL / hash dedupe in DB
        existing = (
            db.query(m.GatherItem)
            .filter_by(tenant_id=tenant_id, content_hash=norm["content_hash"])
            .first()
        )
        if existing:
            pub = _item_public(existing)
            results_out.append(pub)
            if existing.needs_judgment and existing.status != "discarded":
                needs_j.append(pub)
            continue

        rel = code_relevance(
            norm["title"],
            norm["snippet"] or "",
            category,
            query=search_q,
            url=norm.get("url") or "",
            domain_kind=domain_kind,
        )
        item = m.GatherItem(
            id=_uid("gitm_"),
            tenant_id=tenant_id,
            gather_job_id=job.id,
            source=norm["source"],
            category=norm["category"] or "",
            external_id=str(norm.get("external_id") or "")[:200],
            url=norm.get("url"),
            title=norm["title"],
            brand=norm.get("brand"),
            creator=norm.get("creator"),
            snippet=norm.get("snippet"),
            content_text=norm.get("content_text"),
            raw_json=json.dumps(norm.get("raw") or {}),
            content_hash=norm["content_hash"],
            relevance_score=rel["relevance_score"],
            needs_judgment=rel["needs_judgment"],
            status="gathered" if rel["needs_judgment"] else "discarded",
        )
        db.add(item)
        db.flush()
        pub = _item_public(item)
        results_out.append(pub)
        if rel["needs_judgment"]:
            needs_j.append(pub)
        else:
            discarded += 1

    # record search for dedupe window
    db.add(
        m.RecentSearch(
            id=_uid("rs_"),
            tenant_id=tenant_id,
            source=source,
            query=search_q,
            category=category,
        )
    )

    job.status = "completed"
    job.item_count = len(results_out)
    job.needs_judgment_count = len(needs_j)
    job.finished_at = _now()
    db.commit()

    apify_cost = float(job.cost_usd or 0.0)
    return {
        "job_id": job.id,
        "from_cache": bool(cached or job.from_cache),
        "result_count": len(results_out),
        "results": results_out,
        # ONLY these should ever be sent toward OpenRouter judges
        "needs_judgment": needs_j,
        "discarded_low_relevance": discarded,
        "gatherer": "apify",
        "apify_cost_usd": apify_cost,
        "apify": {
            "run_id": job.apify_run_id,
            "dataset_id": job.apify_dataset_id,
            "cost_usd": apify_cost,
        },
        "message": (
            f"Gathered {len(results_out)} items; "
            f"{len(needs_j)} need judgment; {discarded} dropped by code filter"
            + (f"; gather ${apify_cost:.4f}" if apify_cost else " (cache, $0 gather)")
            + "."
        ),
    }


def gather_evidence_for_url(
    db: Session,
    *,
    tenant_id: str,
    url: str,
    existing_item: m.GatherItem | None = None,
) -> dict[str, Any]:
    """Fetch full page content via Apify — LLM must never invent this."""
    settings = get_settings()
    if existing_item and existing_item.content_text and len(existing_item.content_text) > 200:
        return {
            "ok": True,
            "from_cache": True,
            "content": {
                "title": existing_item.title,
                "url": existing_item.url,
                "brand": existing_item.brand,
                "creator": existing_item.creator,
                "source": existing_item.source,
                "full_text": existing_item.content_text,
                "snippet": existing_item.snippet,
            },
            "gatherer": "apify_cache",
        }

    # Check any gather item with this URL already has body
    if url:
        prior = (
            db.query(m.GatherItem)
            .filter_by(tenant_id=tenant_id, url=url)
            .order_by(m.GatherItem.created_at.desc())
            .first()
        )
        if prior and prior.content_text and len(prior.content_text) > 200:
            return {
                "ok": True,
                "from_cache": True,
                "content": {
                    "title": prior.title,
                    "url": prior.url,
                    "brand": prior.brand,
                    "creator": prior.creator,
                    "source": prior.source,
                    "full_text": prior.content_text,
                    "snippet": prior.snippet,
                },
                "gatherer": "apify_cache",
            }

    if not settings.apify_configured:
        raise RuntimeError("APIFY_API_KEY required for evidence gathering")

    page, meta = apify_client.fetch_page(url)
    text = page.get("text") or page.get("content") or page.get("markdown") or ""
    title = page.get("title") or (existing_item.title if existing_item else "") or url
    cost_usd = float(meta.get("cost_usd") or 0.0)
    if cost_usd > 0:
        tenant = db.query(m.Tenant).filter_by(id=tenant_id).first()
        record_apify_spend(tenant, cost_usd)
    content = {
        "title": title,
        "url": url,
        "brand": existing_item.brand if existing_item else None,
        "creator": existing_item.creator if existing_item else None,
        "source": existing_item.source if existing_item else _source_from_url(url),
        "full_text": str(text)[:12000],
        "snippet": str(text)[:500],
        "apify_run_id": meta.get("run_id"),
    }
    if existing_item:
        existing_item.content_text = content["full_text"]
        existing_item.title = content["title"][:500]
        existing_item.raw_json = json.dumps(page)[:50000]
        db.commit()
    else:
        db.commit()
    return {
        "ok": True,
        "from_cache": False,
        "content": content,
        "gatherer": "apify",
        "apify_cost_usd": cost_usd,
        "apify": meta,
    }


def _item_public(item: m.GatherItem) -> dict[str, Any]:
    return {
        "gather_item_id": item.id,
        "title": item.title,
        "url": item.url,
        "brand": item.brand,
        "creator": item.creator,
        "snippet": item.snippet,
        "source": item.source,
        "category": item.category,
        "relevance_score": item.relevance_score,
        "needs_judgment": item.needs_judgment,
        "status": item.status,
    }


def _site_hint(source: str) -> str:
    return {
        "instagram": "instagram.com",
        "tiktok": "tiktok.com",
        "youtube": "youtube.com",
        "x": "x.com OR twitter.com",
        "blog": "",
    }.get(source, "")


def _source_from_url(url: str) -> str:
    host = urlparse(url or "").netloc.lower()
    if "instagram" in host:
        return "instagram"
    if "tiktok" in host:
        return "tiktok"
    if "youtube" in host or "youtu.be" in host:
        return "youtube"
    if "twitter" in host or host == "x.com" or host.endswith(".x.com"):
        return "x"
    return "blog"
