"""Tenant-scoped tool implementations for Helix agents."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy.orm import Session

from helix.config import get_settings
from helix.db import models as m
from helix.services.brief import get_active_project, project_to_dict, schema_to_dict


def _uid(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _tok(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9_@]+", (text or "").lower()) if len(t) > 2}


def _jaccard(a: str, b: str) -> float:
    ta, tb = _tok(a), _tok(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


class ToolContext:
    def __init__(
        self,
        db: Session,
        tenant_id: str,
        agent_key: str,
        owner_user_id: str | None = None,
    ) -> None:
        self.db = db
        self.tenant_id = tenant_id
        self.agent_key = agent_key
        self.owner_user_id = owner_user_id
        self.settings = get_settings()
        self._discovery_jobs: dict[str, list[dict[str, Any]]] = {}
        self._pods: dict[str, dict[str, Any]] = {}

    def log(self, event_type: str, message: str, payload: dict | None = None) -> str:
        eid = _uid("evt_")
        self.db.add(
            m.EventLog(
                id=eid,
                tenant_id=self.tenant_id,
                agent=self.agent_key,
                event_type=event_type,
                message=message,
                payload_json=json.dumps(payload or {}),
            )
        )
        return eid


# ── Handlers ─────────────────────────────────────────────────────────


def get_research_brief(ctx: ToolContext, **_: Any) -> dict:
    project = get_active_project(ctx.db, ctx.tenant_id)
    if not project:
        return {"brief": None, "error": "No active research brief. Configure one in the console."}
    return {"brief": project_to_dict(project)}


def list_topic_schemas(ctx: ToolContext, **_: Any) -> dict:
    rows = (
        ctx.db.query(m.TopicSchema)
        .filter_by(tenant_id=ctx.tenant_id, is_active=True)
        .order_by(m.TopicSchema.topic)
        .all()
    )
    return {"schemas": [schema_to_dict(r) for r in rows]}


def score_all_active(ctx: ToolContext, **_: Any) -> dict:
    cats = ctx.db.query(m.CategoryState).filter_by(tenant_id=ctx.tenant_id).all()
    sources = (
        ctx.db.query(m.SourceReliability).filter_by(tenant_id=ctx.tenant_id).all()
    )
    scores = []
    for cat in cats:
        gap = max(0, cat.phase_target - cat.verified_count) / max(cat.phase_target, 1)
        for src in sources:
            if src.source == "phyllo":
                continue
            score = round(
                0.45 * gap
                + 0.25 * cat.verification_rate_14d
                + 0.20 * src.reliability
                - 0.10 * min(cat.cost_per_verified_14d, 2.0) / 2.0
                + 0.10 * (1 if cat.weeks_missed_target else 0),
                4,
            )
            scores.append(
                {
                    "category": cat.name,
                    "source": src.source,
                    "priority_score": score,
                    "verified_count": cat.verified_count,
                    "phase_target": cat.phase_target,
                    "weeks_missed_target": cat.weeks_missed_target,
                }
            )
    scores.sort(key=lambda x: x["priority_score"], reverse=True)
    return {"scores": scores[:40], "count": len(scores)}


def write_work_queue(ctx: ToolContext, assignments: list | None = None, **_: Any) -> dict:
    recent = (
        ctx.db.query(m.ReallocationLog)
        .filter(m.ReallocationLog.tenant_id == ctx.tenant_id)
        .order_by(m.ReallocationLog.created_at.desc())
        .first()
    )
    if recent and recent.created_at:
        created = recent.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age = (_now() - created).total_seconds()
        if age < 24 * 3600:
            return {"ok": False, "error": "Reallocation already performed within 24h"}

    assignments = assignments or []
    # close open discovery items
    open_items = (
        ctx.db.query(m.WorkQueueItem)
        .filter_by(tenant_id=ctx.tenant_id, status="open", assigned_agent="discovery")
        .all()
    )
    for item in open_items:
        item.status = "superseded"
        item.updated_at = _now()

    created = []
    for a in assignments[:20]:
        wq = m.WorkQueueItem(
            id=_uid("wq_"),
            tenant_id=ctx.tenant_id,
            category=a.get("category", "beauty"),
            source=a.get("source", "instagram"),
            priority_score=float(a.get("priority_score", 0.5)),
            assigned_agent="discovery",
            status="open",
        )
        ctx.db.add(wq)
        created.append(wq.id)

    ctx.db.add(m.ReallocationLog(id=_uid("ra_"), tenant_id=ctx.tenant_id))
    return {"ok": True, "created": created, "count": len(created)}


def get_open_contradictions(ctx: ToolContext, **_: Any) -> dict:
    rows = (
        ctx.db.query(m.Contradiction)
        .filter_by(tenant_id=ctx.tenant_id, resolution_status="open")
        .all()
    )
    return {
        "contradictions": [
            {
                "id": r.id,
                "fact_a_id": r.fact_a_id,
                "fact_b_id": r.fact_b_id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }


def apply_auto_resolution(
    ctx: ToolContext, contradiction_id: str = "", rule: str = "", note: str = "", **_: Any
) -> dict:
    row = (
        ctx.db.query(m.Contradiction)
        .filter_by(id=contradiction_id, tenant_id=ctx.tenant_id)
        .first()
    )
    if not row:
        return {"ok": False, "error": "not found"}
    if rule not in {"recency", "source_reliability"}:
        return {"ok": False, "error": "rule must be recency or source_reliability"}
    row.resolution_status = "auto_resolved"
    row.resolution_note = f"{rule}: {note}"
    row.updated_at = _now()
    return {"ok": True, "id": row.id, "status": row.resolution_status}


def create_escalation(
    ctx: ToolContext, kind: str = "", message: str = "", payload: dict | None = None, **_: Any
) -> dict:
    eid = _uid("esc_")
    ctx.db.add(
        m.Escalation(
            id=eid,
            tenant_id=ctx.tenant_id,
            source_agent=ctx.agent_key,
            kind=kind or "general",
            payload_json=json.dumps({"message": message, **(payload or {})}),
            status="open",
        )
    )
    return {"ok": True, "escalation_id": eid}


def write_research_journal(ctx: ToolContext, entry: str = "", **_: Any) -> dict:
    jid = _uid("rj_")
    ctx.db.add(
        m.ResearchJournal(id=jid, tenant_id=ctx.tenant_id, entry=entry)
    )
    return {"ok": True, "id": jid}


def get_current_assignment(ctx: ToolContext, **_: Any) -> dict:
    item = (
        ctx.db.query(m.WorkQueueItem)
        .filter_by(tenant_id=ctx.tenant_id, status="open", assigned_agent="discovery")
        .order_by(m.WorkQueueItem.priority_score.desc())
        .first()
    )
    if not item:
        return {"assignment": None}
    return {
        "assignment": {
            "id": item.id,
            "category": item.category,
            "source": item.source,
            "priority_score": item.priority_score,
        }
    }


def check_recent_searches(
    ctx: ToolContext, source: str = "", query: str = "", **_: Any
) -> dict:
    q = ctx.db.query(m.RecentSearch).filter_by(tenant_id=ctx.tenant_id, source=source)
    rows = q.order_by(m.RecentSearch.created_at.desc()).limit(50).all()
    dup = False
    if query:
        ql = query.lower()
        dup = any(r.query.lower() == ql for r in rows)
    return {
        "recent": [{"query": r.query, "category": r.category} for r in rows[:10]],
        "is_duplicate": dup,
    }


def trigger_discovery(
    ctx: ToolContext, category: str = "", source: str = "", query: str = "", **_: Any
) -> dict:
    """GATHER via Apify only — never invent results in the LLM."""
    from helix.services.gather.smart import gather_search

    try:
        out = gather_search(
            ctx.db,
            tenant_id=ctx.tenant_id,
            category=category,
            source=source or "blog",
            query=query or category,
            max_results=ctx.settings.apify_max_results_per_search,
        )
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "error": str(e),
            "job_id": None,
            "results": [],
            "needs_judgment": [],
            "gatherer": "apify",
            "message": "Gather failed. Do NOT invent candidates. Escalate or retry later.",
        }
    # Keep in-memory pointer for get_discovery_results compatibility
    job_id = out["job_id"]
    # Only judgment-worthy items for downstream write
    ctx._discovery_jobs[job_id] = out.get("needs_judgment") or []
    return {
        "ok": True,
        "job_id": job_id,
        "result_count": out.get("result_count", 0),
        "needs_judgment_count": len(out.get("needs_judgment") or []),
        "discarded_low_relevance": out.get("discarded_low_relevance", 0),
        "from_cache": out.get("from_cache", False),
        "gatherer": "apify",
        "message": out.get("message"),
        # convenience: same as get_discovery_results
        "results": out.get("needs_judgment") or [],
    }


def get_discovery_results(ctx: ToolContext, job_id: str = "", **_: Any) -> dict:
    # Prefer live memory; else reload needs_judgment items from DB for this job
    if job_id in ctx._discovery_jobs:
        results = ctx._discovery_jobs[job_id]
    else:
        rows = (
            ctx.db.query(m.GatherItem)
            .filter_by(tenant_id=ctx.tenant_id, gather_job_id=job_id, needs_judgment=True)
            .all()
        )
        results = [
            {
                "gather_item_id": r.id,
                "title": r.title,
                "url": r.url,
                "brand": r.brand,
                "creator": r.creator,
                "snippet": r.snippet,
                "source": r.source,
                "category": r.category,
                "relevance_score": r.relevance_score,
                "needs_judgment": r.needs_judgment,
            }
            for r in rows
        ]
    return {
        "job_id": job_id,
        "results": results,
        "gatherer": "apify",
        "note": "Results are Apify-gathered + code-filtered. Do not invent items.",
    }


def score_relevance(
    ctx: ToolContext, title: str = "", category: str = "", snippet: str = "", **_: Any
) -> dict:
    """Code-only relevance (not OpenRouter). Used for double-check; gather already filtered."""
    from helix.services.gather.smart import code_relevance

    return code_relevance(title, snippet, category)


def write_discovery_candidate(
    ctx: ToolContext,
    category: str = "",
    source: str = "",
    title: str = "",
    url: str = "",
    brand: str = "",
    creator: str = "",
    relevance_score: float = 0.0,
    gather_item_id: str = "",
    **_: Any,
) -> dict:
    """Write a candidate ONLY from Apify gather output (prefer gather_item_id)."""
    gitem = None
    if gather_item_id:
        gitem = (
            ctx.db.query(m.GatherItem)
            .filter_by(id=gather_item_id, tenant_id=ctx.tenant_id)
            .first()
        )
        if not gitem:
            return {"ok": False, "error": "gather_item_id not found — do not invent candidates"}
        if not gitem.needs_judgment:
            return {"ok": False, "error": "item was discarded by code relevance filter"}
        title = gitem.title
        url = gitem.url or ""
        brand = gitem.brand or brand
        creator = gitem.creator or creator
        source = gitem.source or source
        category = gitem.category or category
        relevance_score = gitem.relevance_score

    if relevance_score < ctx.settings.relevance_threshold:
        return {"ok": False, "error": "below relevance threshold", "score": relevance_score}
    if not url and not gather_item_id:
        return {
            "ok": False,
            "error": "Candidates must come from Apify gather results (url or gather_item_id required)",
        }
    # Dedupe by URL
    if url:
        existing = (
            ctx.db.query(m.DiscoveryCandidate)
            .filter_by(tenant_id=ctx.tenant_id, url=url)
            .first()
        )
        if existing:
            return {"ok": True, "candidate_id": existing.id, "deduped": True}
    cid = _uid("cand_")
    ctx.db.add(
        m.DiscoveryCandidate(
            id=cid,
            tenant_id=ctx.tenant_id,
            category=category,
            source=source,
            title=title,
            url=url,
            brand=brand,
            creator=creator,
            relevance_score=relevance_score,
            status="pending",
        )
    )
    if gitem:
        gitem.status = "staged"
    return {"ok": True, "candidate_id": cid, "gather_item_id": gather_item_id or None}


def record_search(
    ctx: ToolContext, source: str = "", query: str = "", category: str = "", **_: Any
) -> dict:
    rid = _uid("rs_")
    ctx.db.add(
        m.RecentSearch(
            id=rid,
            tenant_id=ctx.tenant_id,
            source=source,
            query=query,
            category=category,
        )
    )
    return {"ok": True, "id": rid}


def claim_candidate(ctx: ToolContext, candidate_id: str = "", **_: Any) -> dict:
    row = (
        ctx.db.query(m.DiscoveryCandidate)
        .filter_by(id=candidate_id, tenant_id=ctx.tenant_id)
        .first()
    )
    if not row:
        return {"ok": False, "error": "not found"}
    if row.status != "pending":
        return {"ok": False, "error": f"status is {row.status}"}
    row.status = "claimed"
    row.claimed_by = ctx.agent_key
    return {"ok": True, "candidate": {"id": row.id, "title": row.title, "brand": row.brand}}


def collect_full_evidence(ctx: ToolContext, candidate_id: str = "", **_: Any) -> dict:
    """GATHER evidence via Apify/cache only — never fabricate post content."""
    from helix.services.gather.smart import gather_evidence_for_url

    row = (
        ctx.db.query(m.DiscoveryCandidate)
        .filter_by(id=candidate_id, tenant_id=ctx.tenant_id)
        .first()
    )
    if not row:
        return {"ok": False, "error": "not found"}
    if not row.url:
        return {
            "ok": False,
            "error": "Candidate has no URL from Apify gather — cannot invent evidence",
            "complete": False,
        }
    gitem = (
        ctx.db.query(m.GatherItem)
        .filter_by(tenant_id=ctx.tenant_id, url=row.url)
        .order_by(m.GatherItem.created_at.desc())
        .first()
    )
    try:
        out = gather_evidence_for_url(
            ctx.db,
            tenant_id=ctx.tenant_id,
            url=row.url,
            existing_item=gitem,
        )
    except Exception as e:  # noqa: BLE001
        return {
            "ok": False,
            "error": str(e),
            "complete": False,
            "message": "Apify evidence fetch failed. Do NOT invent content.",
        }
    content = out.get("content") or {}
    # fill identity from candidate if page lacks it
    content.setdefault("brand", row.brand)
    content.setdefault("creator", row.creator)
    content.setdefault("source", row.source)
    complete = bool(content.get("full_text") and len(content["full_text"]) > 40)
    return {
        "ok": True,
        "content": content,
        "complete": complete,
        "gatherer": out.get("gatherer"),
        "from_cache": out.get("from_cache", False),
        "note": "Evidence from Apify/cache only.",
    }


def assess_completeness(
    ctx: ToolContext, candidate_id: str = "", content: str | dict | None = None, **_: Any
) -> dict:
    text = content if isinstance(content, str) else json.dumps(content or {})
    complete = len(text) > 40
    return {"complete": complete, "reason": None if complete else "thin content"}


def write_to_raw_lake(
    ctx: ToolContext, candidate_id: str = "", content: dict | None = None, **_: Any
) -> dict:
    rid = _uid("raw_")
    ctx.db.add(
        m.RawLakeItem(
            id=rid,
            tenant_id=ctx.tenant_id,
            candidate_id=candidate_id,
            content_json=json.dumps(content or {}),
        )
    )
    return {"ok": True, "raw_id": rid}


def compute_preliminary_confidence(
    ctx: ToolContext, candidate_id: str = "", content_length: int = 0, **_: Any
) -> dict:
    conf = min(0.4 + content_length / 500, 0.75)
    return {"preliminary_confidence": round(conf, 3), "note": "heuristic only"}


def extract_lightweight_signals(
    ctx: ToolContext,
    candidate_id: str = "",
    brand: str = "",
    creator: str = "",
    content_date: str = "",
    **_: Any,
) -> dict:
    row = (
        ctx.db.query(m.DiscoveryCandidate)
        .filter_by(id=candidate_id, tenant_id=ctx.tenant_id)
        .first()
    )
    signals = {
        "brand": brand or (row.brand if row else None),
        "creator": creator or (row.creator if row else None),
        "content_date": content_date or (row.content_date if row else None),
    }
    return {"signals": signals}


def write_evidence_staging(
    ctx: ToolContext,
    candidate_id: str = "",
    brand: str = "",
    creator: str = "",
    content_date: str = "",
    content_text: str = "",
    preliminary_confidence: float = 0.5,
    identity_signals: dict | None = None,
    **_: Any,
) -> dict:
    sid = _uid("stg_")
    ctx.db.add(
        m.EvidenceStaging(
            id=sid,
            tenant_id=ctx.tenant_id,
            candidate_id=candidate_id,
            brand=brand,
            creator=creator,
            content_date=content_date,
            content_text=content_text,
            preliminary_confidence=preliminary_confidence,
            identity_signals_json=json.dumps(identity_signals or {}),
            status="pending_dedup",
        )
    )
    cand = (
        ctx.db.query(m.DiscoveryCandidate)
        .filter_by(id=candidate_id, tenant_id=ctx.tenant_id)
        .first()
    )
    if cand:
        cand.status = "staged"
    return {"ok": True, "staging_id": sid}


def discard_candidate(ctx: ToolContext, candidate_id: str = "", reason: str = "", **_: Any) -> dict:
    row = (
        ctx.db.query(m.DiscoveryCandidate)
        .filter_by(id=candidate_id, tenant_id=ctx.tenant_id)
        .first()
    )
    if not row:
        return {"ok": False, "error": "not found"}
    row.status = "discarded"
    ctx.log("discard", reason, {"candidate_id": candidate_id})
    return {"ok": True}


def get_pending_dedup_batch(ctx: ToolContext, **_: Any) -> dict:
    rows = (
        ctx.db.query(m.EvidenceStaging)
        .filter_by(tenant_id=ctx.tenant_id, status="pending_dedup")
        .limit(20)
        .all()
    )
    return {
        "batch": [
            {
                "id": r.id,
                "brand": r.brand,
                "creator": r.creator,
                "content_text": (r.content_text or "")[:500],
                "preliminary_confidence": r.preliminary_confidence,
            }
            for r in rows
        ]
    }


def compute_content_similarity(
    ctx: ToolContext, staging_id: str = "", content_text: str = "", **_: Any
) -> dict:
    stg = (
        ctx.db.query(m.EvidenceStaging)
        .filter_by(id=staging_id, tenant_id=ctx.tenant_id)
        .first()
    )
    text = content_text or (stg.content_text if stg else "") or ""
    camps = ctx.db.query(m.Campaign).filter_by(tenant_id=ctx.tenant_id).all()
    best = {"campaign_id": None, "similarity": 0.0}
    for c in camps:
        blob = f"{c.title} {c.brand} {c.creator}"
        sim = _jaccard(text, blob)
        if sim > best["similarity"]:
            best = {"campaign_id": c.id, "similarity": round(sim, 3)}
    return best


def get_campaign_identity_signals(
    ctx: ToolContext, brand: str = "", creator: str = "", **_: Any
) -> dict:
    q = ctx.db.query(m.Campaign).filter_by(tenant_id=ctx.tenant_id)
    if brand:
        q = q.filter(m.Campaign.brand == brand)
    if creator:
        q = q.filter(m.Campaign.creator == creator)
    rows = q.limit(20).all()
    return {
        "matches": [
            {
                "id": r.id,
                "brand": r.brand,
                "creator": r.creator,
                "title": r.title,
                "verification_status": r.verification_status,
            }
            for r in rows
        ]
    }


def compute_match_score(
    ctx: ToolContext,
    staging_id: str = "",
    content_similarity: float = 0.0,
    brand_match: bool = False,
    creator_match: bool = False,
    date_overlap: bool = False,
    **_: Any,
) -> dict:
    score = 0.0
    if brand_match and creator_match:
        score += 0.55
    elif brand_match or creator_match:
        score += 0.25
    score += 0.35 * float(content_similarity)
    if date_overlap:
        score += 0.1
    score = round(min(score, 0.99), 3)
    band = (
        "high"
        if score >= ctx.settings.match_high
        else "low"
        if score < ctx.settings.match_low
        else "ambiguous"
    )
    return {"match_score": score, "band": band}


def attach_evidence_to_campaign(
    ctx: ToolContext, staging_id: str = "", campaign_id: str = "", **_: Any
) -> dict:
    stg = (
        ctx.db.query(m.EvidenceStaging)
        .filter_by(id=staging_id, tenant_id=ctx.tenant_id)
        .first()
    )
    camp = (
        ctx.db.query(m.Campaign)
        .filter_by(id=campaign_id, tenant_id=ctx.tenant_id)
        .first()
    )
    if not stg or not camp:
        return {"ok": False, "error": "staging or campaign not found"}
    if camp.verification_status == "verified":
        # attaching more evidence to verified is ok; merging two verified is not this path
        pass
    ctx.db.add(
        m.CampaignEvidence(
            id=_uid("ce_"),
            tenant_id=ctx.tenant_id,
            campaign_id=campaign_id,
            staging_id=staging_id,
            content_text=stg.content_text,
            confidence=stg.preliminary_confidence,
        )
    )
    stg.status = "attached"
    return {"ok": True, "campaign_id": campaign_id}


def create_campaign_stub(
    ctx: ToolContext,
    staging_id: str = "",
    category: str = "",
    title: str = "",
    **_: Any,
) -> dict:
    stg = (
        ctx.db.query(m.EvidenceStaging)
        .filter_by(id=staging_id, tenant_id=ctx.tenant_id)
        .first()
    )
    if not stg:
        return {"ok": False, "error": "staging not found"}
    cid = _uid("camp_")
    ctx.db.add(
        m.Campaign(
            id=cid,
            tenant_id=ctx.tenant_id,
            brand=stg.brand,
            creator=stg.creator,
            category=category or "unknown",
            title=title or (stg.content_text or "")[:120],
            verification_status="pending",
        )
    )
    ctx.db.add(
        m.CampaignEvidence(
            id=_uid("ce_"),
            tenant_id=ctx.tenant_id,
            campaign_id=cid,
            staging_id=staging_id,
            content_text=stg.content_text,
            confidence=stg.preliminary_confidence,
        )
    )
    stg.status = "new_campaign"
    return {"ok": True, "campaign_id": cid}


def flag_ambiguous_match(
    ctx: ToolContext,
    staging_id: str = "",
    match_score: float = 0.0,
    candidate_campaign_id: str = "",
    reasoning: str = "",
    **_: Any,
) -> dict:
    mid = _uid("amb_")
    ctx.db.add(
        m.AmbiguousMatch(
            id=mid,
            tenant_id=ctx.tenant_id,
            staging_id=staging_id,
            match_score=match_score,
            candidate_campaign_id=candidate_campaign_id or None,
            reasoning=reasoning,
            status="open",
        )
    )
    stg = (
        ctx.db.query(m.EvidenceStaging)
        .filter_by(id=staging_id, tenant_id=ctx.tenant_id)
        .first()
    )
    if stg:
        stg.status = "ambiguous"
    return {"ok": True, "match_id": mid}


def get_pending_verification_batch(ctx: ToolContext, **_: Any) -> dict:
    rows = (
        ctx.db.query(m.Campaign)
        .filter(
            m.Campaign.tenant_id == ctx.tenant_id,
            m.Campaign.verification_status.in_(["pending", "request_more_evidence"]),
        )
        .limit(20)
        .all()
    )
    out = []
    for r in rows:
        ev = (
            ctx.db.query(m.CampaignEvidence)
            .filter_by(tenant_id=ctx.tenant_id, campaign_id=r.id)
            .all()
        )
        out.append(
            {
                "id": r.id,
                "brand": r.brand,
                "creator": r.creator,
                "title": r.title,
                "category": r.category,
                "status": r.verification_status,
                "more_evidence_cycles": r.more_evidence_cycles,
                "evidence": [
                    {"source": e.source, "text": (e.content_text or "")[:300], "confidence": e.confidence}
                    for e in ev
                ],
            }
        )
    return {"batch": out}


def get_source_reliability(ctx: ToolContext, source: str = "", **_: Any) -> dict:
    row = (
        ctx.db.query(m.SourceReliability)
        .filter_by(tenant_id=ctx.tenant_id, source=source)
        .first()
    )
    return {"source": source, "reliability": row.reliability if row else 0.5}


def check_phyllo_profile_consistency(
    ctx: ToolContext, creator: str = "", brand: str = "", **_: Any
) -> dict:
    # Simulated independent profile check
    consistent = bool(creator) and not creator.startswith("@unknown")
    return {
        "creator": creator,
        "profile_exists": consistent,
        "audience_overlap_with_brand": 0.62 if consistent else 0.1,
        "consistent": consistent,
    }


def get_ambiguous_match_flag(ctx: ToolContext, staging_id: str = "", **_: Any) -> dict:
    q = ctx.db.query(m.AmbiguousMatch).filter_by(tenant_id=ctx.tenant_id, status="open")
    if staging_id:
        q = q.filter_by(staging_id=staging_id)
    row = q.first()
    if not row:
        return {"flag": None}
    return {
        "flag": {
            "id": row.id,
            "staging_id": row.staging_id,
            "match_score": row.match_score,
            "candidate_campaign_id": row.candidate_campaign_id,
            "reasoning": row.reasoning,
        }
    }


def resolve_ambiguous_match(
    ctx: ToolContext,
    match_id: str = "",
    resolution: str = "",
    campaign_id: str = "",
    reasoning: str = "",
    **_: Any,
) -> dict:
    row = (
        ctx.db.query(m.AmbiguousMatch)
        .filter_by(id=match_id, tenant_id=ctx.tenant_id)
        .first()
    )
    if not row:
        return {"ok": False, "error": "not found"}
    row.status = f"resolved_{resolution}"
    row.reasoning = (row.reasoning or "") + f" | {reasoning}"
    return {"ok": True, "id": row.id, "status": row.status}


def escalate_ambiguous_match(
    ctx: ToolContext, match_id: str = "", reason: str = "", **_: Any
) -> dict:
    return create_escalation(
        ctx,
        kind="ambiguous_match",
        message=reason,
        payload={"match_id": match_id},
    )


def update_verification_status(
    ctx: ToolContext,
    campaign_id: str = "",
    status: str = "",
    confidence: float = 0.0,
    reasoning: str = "",
    **_: Any,
) -> dict:
    camp = (
        ctx.db.query(m.Campaign)
        .filter_by(id=campaign_id, tenant_id=ctx.tenant_id)
        .first()
    )
    if not camp:
        return {"ok": False, "error": "not found"}
    if status == "request_more_evidence":
        if camp.more_evidence_cycles >= 2:
            return {"ok": False, "error": "max request_more_evidence cycles reached"}
        camp.more_evidence_cycles += 1
    camp.verification_status = status
    camp.confidence = confidence
    camp.verification_reasoning = reasoning
    camp.updated_at = _now()
    if status == "verified":
        cat = (
            ctx.db.query(m.CategoryState)
            .filter_by(tenant_id=ctx.tenant_id, name=camp.category or "")
            .first()
        )
        if cat:
            cat.verified_count += 1
    return {"ok": True, "campaign_id": camp.id, "status": status, "confidence": confidence}


def get_unextracted_verified_campaigns(ctx: ToolContext, **_: Any) -> dict:
    verified = (
        ctx.db.query(m.Campaign)
        .filter_by(tenant_id=ctx.tenant_id, verification_status="verified")
        .all()
    )
    out = []
    for c in verified:
        has_pending = (
            ctx.db.query(m.CandidateFact)
            .filter_by(tenant_id=ctx.tenant_id, campaign_id=c.id, status="pending")
            .count()
        )
        has_graph = (
            ctx.db.query(m.GraphFact)
            .filter_by(tenant_id=ctx.tenant_id, campaign_id=c.id)
            .count()
        )
        if has_graph == 0 or has_pending > 0:
            out.append(
                {
                    "id": c.id,
                    "brand": c.brand,
                    "creator": c.creator,
                    "title": c.title,
                    "category": c.category,
                    "graph_facts": has_graph,
                }
            )
    return {"campaigns": out[:20]}


def get_campaign_evidence_content(ctx: ToolContext, campaign_id: str = "", **_: Any) -> dict:
    rows = (
        ctx.db.query(m.CampaignEvidence)
        .filter_by(tenant_id=ctx.tenant_id, campaign_id=campaign_id)
        .all()
    )
    return {
        "evidence": [
            {"id": r.id, "source": r.source, "text": r.content_text, "confidence": r.confidence}
            for r in rows
        ]
    }


def get_ontology(ctx: ToolContext, **_: Any) -> dict:
    rows = ctx.db.query(m.OntologyType).filter_by(tenant_id=ctx.tenant_id).all()
    return {
        "ontology": [
            {"type_name": r.type_name, "kind": r.kind, "description": r.description}
            for r in rows
        ]
    }


def extract_entities(
    ctx: ToolContext, campaign_id: str = "", entities: list | None = None, **_: Any
) -> dict:
    return {"ok": True, "recorded": len(entities or []), "note": "use write_candidate_fact to persist"}


def extract_relationships(
    ctx: ToolContext, campaign_id: str = "", relationships: list | None = None, **_: Any
) -> dict:
    return {"ok": True, "recorded": len(relationships or []), "note": "use write_candidate_fact to persist"}


def score_extraction_confidence(
    ctx: ToolContext, text_support: str = "", is_inferred: bool = False, **_: Any
) -> dict:
    base = 0.85 if len(text_support) > 20 else 0.45
    if is_inferred:
        base = min(base, 0.65)
    return {"extraction_confidence": round(base, 3)}


def write_candidate_fact(
    ctx: ToolContext,
    campaign_id: str = "",
    entity: str = "",
    fact_type: str = "",
    value: str = "",
    relationship: str = "",
    citation: str = "",
    is_inferred: bool = False,
    extraction_confidence: float = 0.5,
    **_: Any,
) -> dict:
    if not citation:
        return {"ok": False, "error": "citation required"}
    ont = (
        ctx.db.query(m.OntologyType)
        .filter_by(tenant_id=ctx.tenant_id, type_name=fact_type)
        .first()
    )
    if not ont:
        # also allow entity types as fact_type labels
        ont = (
            ctx.db.query(m.OntologyType)
            .filter_by(tenant_id=ctx.tenant_id, type_name=entity)
            .first()
        )
    fid = _uid("cf_")
    ctx.db.add(
        m.CandidateFact(
            id=fid,
            tenant_id=ctx.tenant_id,
            campaign_id=campaign_id,
            entity=entity,
            fact_type=fact_type,
            value=value,
            relationship=relationship or None,
            citation=citation,
            is_inferred=is_inferred,
            extraction_confidence=extraction_confidence,
            status="pending",
        )
    )
    if extraction_confidence < 0.5:
        create_escalation(
            ctx,
            kind="low_extraction_confidence",
            message=f"Fact {fid} confidence {extraction_confidence}",
            payload={"candidate_fact_id": fid},
        )
    return {"ok": True, "candidate_fact_id": fid}


def flag_ontology_gap(
    ctx: ToolContext, campaign_id: str = "", description: str = "", **_: Any
) -> dict:
    return create_escalation(
        ctx,
        kind="ontology_gap",
        message=description,
        payload={"campaign_id": campaign_id},
    )


def get_pending_graph_writes(ctx: ToolContext, **_: Any) -> dict:
    rows = (
        ctx.db.query(m.CandidateFact)
        .filter_by(tenant_id=ctx.tenant_id, status="pending")
        .limit(30)
        .all()
    )
    return {
        "facts": [
            {
                "id": r.id,
                "campaign_id": r.campaign_id,
                "entity": r.entity,
                "fact_type": r.fact_type,
                "value": r.value,
                "relationship": r.relationship,
                "citation": r.citation,
                "extraction_confidence": r.extraction_confidence,
            }
            for r in rows
        ]
    }


def query_existing_facts(
    ctx: ToolContext, entity: str = "", fact_type: str = "", **_: Any
) -> dict:
    q = ctx.db.query(m.GraphFact).filter_by(tenant_id=ctx.tenant_id, entity=entity)
    if fact_type:
        q = q.filter_by(fact_type=fact_type)
    rows = q.limit(50).all()
    return {
        "facts": [
            {
                "id": r.id,
                "entity": r.entity,
                "fact_type": r.fact_type,
                "value": r.value,
                "citation": r.citation,
            }
            for r in rows
        ]
    }


def check_conflict(
    ctx: ToolContext, entity: str = "", fact_type: str = "", value: str = "", **_: Any
) -> dict:
    existing = query_existing_facts(ctx, entity=entity, fact_type=fact_type)["facts"]
    conflicts = []
    for f in existing:
        if f["value"] != value:
            # numeric tolerance
            try:
                a = float(re.sub(r"[^0-9.\-]", "", f["value"]))
                b = float(re.sub(r"[^0-9.\-]", "", value))
                if abs(a - b) / max(abs(a), 1e-6) < 0.05:
                    continue
            except ValueError:
                pass
            conflicts.append(f)
    return {"has_conflict": bool(conflicts), "conflicts": conflicts}


def write_graph_fact(
    ctx: ToolContext,
    candidate_fact_id: str = "",
    entity: str = "",
    fact_type: str = "",
    value: str = "",
    relationship: str = "",
    citation: str = "",
    campaign_id: str = "",
    **_: Any,
) -> dict:
    if not citation:
        return {"ok": False, "error": "citation required"}
    gid = _uid("gf_")
    ctx.db.add(
        m.GraphFact(
            id=gid,
            tenant_id=ctx.tenant_id,
            entity=entity,
            fact_type=fact_type,
            value=value,
            relationship=relationship or None,
            citation=citation,
            campaign_id=campaign_id or None,
        )
    )
    if candidate_fact_id:
        cf = (
            ctx.db.query(m.CandidateFact)
            .filter_by(id=candidate_fact_id, tenant_id=ctx.tenant_id)
            .first()
        )
        if cf:
            cf.status = "committed"
    return {"ok": True, "graph_fact_id": gid}


def write_contradiction(
    ctx: ToolContext, fact_a_id: str = "", fact_b_id: str = "", **_: Any
) -> dict:
    cid = _uid("con_")
    ctx.db.add(
        m.Contradiction(
            id=cid,
            tenant_id=ctx.tenant_id,
            fact_a_id=fact_a_id,
            fact_b_id=fact_b_id,
            resolution_status="open",
        )
    )
    return {"ok": True, "contradiction_id": cid}


def reject_candidate_fact(
    ctx: ToolContext, candidate_fact_id: str = "", reason: str = "", **_: Any
) -> dict:
    cf = (
        ctx.db.query(m.CandidateFact)
        .filter_by(id=candidate_fact_id, tenant_id=ctx.tenant_id)
        .first()
    )
    if not cf:
        return {"ok": False, "error": "not found"}
    cf.status = "rejected"
    ctx.log("reject_fact", reason, {"candidate_fact_id": candidate_fact_id})
    return {"ok": True}


def get_analysis_candidates(ctx: ToolContext, **_: Any) -> dict:
    cats = ctx.db.query(m.CategoryState).filter_by(tenant_id=ctx.tenant_id).all()
    out = []
    for cat in cats:
        n = (
            ctx.db.query(m.Campaign)
            .filter_by(
                tenant_id=ctx.tenant_id,
                category=cat.name,
                verification_status="verified",
            )
            .count()
        )
        out.append(
            {
                "cluster_key": cat.name,
                "verified_count": n,
                "ready": n >= 2,
            }
        )
    return {"clusters": out}


def get_cluster_campaigns(ctx: ToolContext, cluster_key: str = "", **_: Any) -> dict:
    rows = (
        ctx.db.query(m.Campaign)
        .filter_by(
            tenant_id=ctx.tenant_id,
            category=cluster_key,
            verification_status="verified",
        )
        .all()
    )
    return {
        "campaigns": [
            {
                "id": r.id,
                "brand": r.brand,
                "creator": r.creator,
                "title": r.title,
                "confidence": r.confidence,
            }
            for r in rows
        ]
    }


def get_research_memory(ctx: ToolContext, topic: str = "", **_: Any) -> dict:
    q = ctx.db.query(m.ResearchMemory).filter(m.ResearchMemory.tenant_id == ctx.tenant_id)
    if topic:
        q = q.filter(m.ResearchMemory.topic.contains(topic))
    rows = q.all()
    return {
        "memory": [
            {"id": r.id, "topic": r.topic, "summary": r.summary} for r in rows
        ]
    }


def write_strategic_analysis(
    ctx: ToolContext,
    cluster_key: str = "",
    analysis: dict | None = None,
    confidence: float = 0.5,
    **_: Any,
) -> dict:
    aid = _uid("sa_")
    ctx.db.add(
        m.StrategicAnalysis(
            id=aid,
            tenant_id=ctx.tenant_id,
            cluster_key=cluster_key,
            analysis_json=json.dumps(analysis or {}),
            confidence=confidence,
        )
    )
    return {"ok": True, "id": aid}


def write_research_memory_summary(
    ctx: ToolContext, topic: str = "", summary: str = "", **_: Any
) -> dict:
    row = (
        ctx.db.query(m.ResearchMemory)
        .filter_by(tenant_id=ctx.tenant_id, topic=topic)
        .first()
    )
    if row:
        row.summary = summary
        row.updated_at = _now()
        return {"ok": True, "id": row.id, "updated": True}
    rid = _uid("rm_")
    ctx.db.add(
        m.ResearchMemory(id=rid, tenant_id=ctx.tenant_id, topic=topic, summary=summary)
    )
    return {"ok": True, "id": rid, "updated": False}


def get_seed_material(ctx: ToolContext, **_: Any) -> dict:
    rows = ctx.db.query(m.SeedMaterial).filter_by(tenant_id=ctx.tenant_id).all()
    return {
        "seeds": [
            {
                "id": r.id,
                "topic": r.topic,
                "input_text": r.input_text,
                "output_text": r.output_text,
                "reasoning_pattern": r.reasoning_pattern,
            }
            for r in rows
        ]
    }


def get_topic_schema(ctx: ToolContext, topic: str = "", **_: Any) -> dict:
    row = (
        ctx.db.query(m.TopicSchema)
        .filter_by(tenant_id=ctx.tenant_id, topic=topic)
        .first()
    )
    if not row:
        return {"schema": None, "error": f"No topic schema for '{topic}'"}
    return schema_to_dict(row)


def generate_variation(ctx: ToolContext, **kwargs: Any) -> dict:
    return {"ok": True, "variation": kwargs, "note": "persist via write_draft_training_example"}


def validate_business_logic_invariant(
    ctx: ToolContext, seed_id: str = "", rationale: str = "", **_: Any
) -> dict:
    valid = len(rationale or "") > 20
    return {"valid": valid, "reason": None if valid else "rationale too weak"}


def write_draft_training_example(
    ctx: ToolContext,
    seed_id: str = "",
    topic: str = "",
    input_text: str = "",
    output_text: str = "",
    difficulty: str = "moderate",
    is_negative: bool = False,
    rationale: str = "",
    **_: Any,
) -> dict:
    eid = _uid("ex_")
    ctx.db.add(
        m.TrainingExample(
            id=eid,
            tenant_id=ctx.tenant_id,
            owner_user_id=ctx.owner_user_id,
            seed_id=seed_id or None,
            topic=topic,
            input_text=input_text,
            output_text=output_text,
            difficulty=difficulty,
            is_negative=is_negative,
            rationale=rationale,
            review_status="draft",
        )
    )
    return {"ok": True, "example_id": eid}


def get_pending_review_batch(ctx: ToolContext, **_: Any) -> dict:
    rows = (
        ctx.db.query(m.TrainingExample)
        .filter_by(tenant_id=ctx.tenant_id, review_status="draft")
        .limit(20)
        .all()
    )
    return {
        "batch": [
            {
                "id": r.id,
                "topic": r.topic,
                "input_text": r.input_text,
                "output_text": r.output_text,
                "difficulty": r.difficulty,
                "is_negative": r.is_negative,
                "rationale": r.rationale,
            }
            for r in rows
        ]
    }


def construct_counter_argument(
    ctx: ToolContext,
    example_id: str = "",
    counter: str = "",
    defeats_original: bool = False,
    **_: Any,
) -> dict:
    return {
        "example_id": example_id,
        "counter": counter,
        "defeats_original": defeats_original,
    }


def reassess_difficulty(
    ctx: ToolContext, example_id: str = "", difficulty: str = "", **_: Any
) -> dict:
    row = (
        ctx.db.query(m.TrainingExample)
        .filter_by(id=example_id, tenant_id=ctx.tenant_id)
        .first()
    )
    if row and difficulty:
        row.difficulty = difficulty
    return {"ok": True, "difficulty": difficulty}


def check_negative_example_validity(
    ctx: ToolContext, example_id: str = "", valid: bool = True, note: str = "", **_: Any
) -> dict:
    return {"example_id": example_id, "valid": valid, "note": note}


def fact_check_against_graph(
    ctx: ToolContext, example_id: str = "", query: str = "", **_: Any
) -> dict:
    facts = (
        ctx.db.query(m.GraphFact)
        .filter_by(tenant_id=ctx.tenant_id)
        .limit(20)
        .all()
    )
    return {
        "example_id": example_id,
        "related_facts": [
            {"entity": f.entity, "fact_type": f.fact_type, "value": f.value}
            for f in facts
            if not query or query.lower() in f.value.lower() or query.lower() in f.entity.lower()
        ][:10],
    }


def update_review_status(
    ctx: ToolContext,
    example_id: str = "",
    status: str = "",
    reasoning: str = "",
    **_: Any,
) -> dict:
    row = (
        ctx.db.query(m.TrainingExample)
        .filter_by(id=example_id, tenant_id=ctx.tenant_id)
        .first()
    )
    if not row:
        return {"ok": False, "error": "not found"}
    row.review_status = status
    row.review_reasoning = reasoning
    promoted_gold_id = None
    if status == "approved" and ctx.owner_user_id:
        from helix.services.library import get_or_create_scope, promote_training_example_to_gold

        if not row.owner_user_id:
            row.owner_user_id = ctx.owner_user_id
        scope = get_or_create_scope(ctx.db, ctx.owner_user_id, ctx.tenant_id)
        if scope.auto_promote_approved and not row.reserved_for_benchmark:
            gold = promote_training_example_to_gold(ctx.db, row, ctx.owner_user_id)
            if gold:
                promoted_gold_id = gold.id
    return {
        "ok": True,
        "id": row.id,
        "status": status,
        "promoted_to_user_gold": promoted_gold_id,
    }


def get_approved_examples_pool(ctx: ToolContext, **_: Any) -> dict:
    rows = (
        ctx.db.query(m.TrainingExample)
        .filter_by(tenant_id=ctx.tenant_id, review_status="approved")
        .all()
    )
    return {
        "examples": [
            {
                "id": r.id,
                "topic": r.topic,
                "difficulty": r.difficulty,
                "reserved_for_benchmark": r.reserved_for_benchmark,
                "split": r.split,
            }
            for r in rows
        ]
    }


def get_current_benchmark_composition(ctx: ToolContext, **_: Any) -> dict:
    rows = (
        ctx.db.query(m.TrainingExample)
        .filter_by(tenant_id=ctx.tenant_id, reserved_for_benchmark=True)
        .all()
    )
    by_diff: dict[str, int] = {}
    by_topic: dict[str, int] = {}
    for r in rows:
        by_diff[r.difficulty] = by_diff.get(r.difficulty, 0) + 1
        by_topic[r.topic] = by_topic.get(r.topic, 0) + 1
    return {"count": len(rows), "by_difficulty": by_diff, "by_topic": by_topic}


def claim_for_benchmark(ctx: ToolContext, example_id: str = "", **_: Any) -> dict:
    row = (
        ctx.db.query(m.TrainingExample)
        .filter_by(id=example_id, tenant_id=ctx.tenant_id)
        .first()
    )
    if not row:
        return {"ok": False, "error": "not found"}
    if row.dataset_version:
        return {"ok": False, "error": "already in a dataset version"}
    row.reserved_for_benchmark = True
    return {"ok": True, "example_id": example_id}


def get_prior_benchmark_versions(ctx: ToolContext, **_: Any) -> dict:
    rows = (
        ctx.db.query(m.BenchmarkVersion)
        .filter_by(tenant_id=ctx.tenant_id)
        .order_by(m.BenchmarkVersion.created_at.desc())
        .all()
    )
    return {
        "versions": [
            {"id": r.id, "version": r.version, "notes": r.notes} for r in rows
        ]
    }


def create_benchmark_version(
    ctx: ToolContext,
    version: str = "",
    notes: str = "",
    example_ids: list | None = None,
    **_: Any,
) -> dict:
    composition = get_current_benchmark_composition(ctx)
    bid = _uid("bm_")
    ctx.db.add(
        m.BenchmarkVersion(
            id=bid,
            tenant_id=ctx.tenant_id,
            version=version,
            composition_json=json.dumps(
                {"stats": composition, "example_ids": example_ids or []}
            ),
            notes=notes,
        )
    )
    return {"ok": True, "id": bid, "version": version}


def flag_thin_coverage(ctx: ToolContext, topic: str = "", note: str = "", **_: Any) -> dict:
    return create_escalation(
        ctx, kind="thin_benchmark_coverage", message=f"{topic}: {note}", payload={"topic": topic}
    )


def get_approved_non_benchmark_examples(ctx: ToolContext, **_: Any) -> dict:
    rows = (
        ctx.db.query(m.TrainingExample)
        .filter_by(
            tenant_id=ctx.tenant_id,
            review_status="approved",
            reserved_for_benchmark=False,
        )
        .all()
    )
    return {
        "examples": [
            {
                "id": r.id,
                "seed_id": r.seed_id,
                "topic": r.topic,
                "difficulty": r.difficulty,
                "split": r.split,
            }
            for r in rows
        ]
    }


def check_semantic_duplicates(ctx: ToolContext, **_: Any) -> dict:
    rows = (
        ctx.db.query(m.TrainingExample)
        .filter_by(tenant_id=ctx.tenant_id, review_status="approved")
        .all()
    )
    dups = []
    for i, a in enumerate(rows):
        for b in rows[i + 1 :]:
            sim = _jaccard(a.input_text, b.input_text)
            if sim > 0.85:
                dups.append({"a": a.id, "b": b.id, "similarity": round(sim, 3)})
    return {"duplicates": dups[:50]}


def assign_splits(
    ctx: ToolContext,
    train_ratio: float = 0.8,
    val_ratio: float = 0.1,
    test_ratio: float = 0.1,
    **_: Any,
) -> dict:
    rows = (
        ctx.db.query(m.TrainingExample)
        .filter_by(
            tenant_id=ctx.tenant_id,
            review_status="approved",
            reserved_for_benchmark=False,
        )
        .all()
    )
    # group by seed
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r.seed_id or r.id, []).append(r)
    keys = list(groups.keys())
    n = len(keys)
    n_train = int(n * train_ratio)
    n_val = int(n * val_ratio)
    counts = {"train": 0, "validation": 0, "test": 0}
    for i, k in enumerate(keys):
        split = "train" if i < n_train else "validation" if i < n_train + n_val else "test"
        for r in groups[k]:
            r.split = split
            counts[split] += 1
    return {"ok": True, "counts": counts}


def write_training_examples_batch(
    ctx: ToolContext, dataset_version: str = "", example_ids: list | None = None, **_: Any
) -> dict:
    q = (
        ctx.db.query(m.TrainingExample)
        .filter_by(
            tenant_id=ctx.tenant_id,
            review_status="approved",
            reserved_for_benchmark=False,
        )
    )
    rows = q.all()
    if example_ids:
        rows = [r for r in rows if r.id in example_ids]
    for r in rows:
        r.dataset_version = dataset_version
    return {"ok": True, "updated": len(rows)}


def create_dataset_version(
    ctx: ToolContext, version: str = "", manifest: dict | None = None, **_: Any
) -> dict:
    did = _uid("ds_")
    ctx.db.add(
        m.DatasetVersion(
            id=did,
            tenant_id=ctx.tenant_id,
            version=version,
            manifest_json=json.dumps(manifest or {}),
        )
    )
    return {"ok": True, "id": did, "version": version}


def write_manifest(
    ctx: ToolContext, version: str = "", manifest: dict | None = None, **_: Any
) -> dict:
    row = (
        ctx.db.query(m.DatasetVersion)
        .filter_by(tenant_id=ctx.tenant_id, version=version)
        .first()
    )
    if not row:
        return create_dataset_version(ctx, version=version, manifest=manifest)
    row.manifest_json = json.dumps(manifest or {})
    return {"ok": True, "id": row.id}


def flag_undersized_split(
    ctx: ToolContext, topic: str = "", split: str = "", count: int = 0, **_: Any
) -> dict:
    return create_escalation(
        ctx,
        kind="undersized_split",
        message=f"{topic}/{split} has only {count} examples",
        payload={"topic": topic, "split": split, "count": count},
    )


def get_training_run_config(ctx: ToolContext, dataset_version: str = "", **_: Any) -> dict:
    return {
        "dataset_version": dataset_version or "latest",
        "config": {"epochs": 3, "lr": 2e-5, "batch_size": 8, "base": "sim-local"},
    }


def provision_training_pod(ctx: ToolContext, **_: Any) -> dict:
    pod_id = _uid("pod_")
    ctx._pods[pod_id] = {"status": "running"}
    return {"pod_id": pod_id, "status": "running", "mode": "simulated"}


def run_training_job(
    ctx: ToolContext,
    pod_id: str = "",
    dataset_version: str = "",
    config: dict | None = None,
    **_: Any,
) -> dict:
    model_id = _uid("mdl_")
    run_id = _uid("run_")
    ctx.db.add(
        m.TrainingRun(
            id=run_id,
            tenant_id=ctx.tenant_id,
            dataset_version=dataset_version or "ds_sim",
            config_json=json.dumps(config or {}),
            status="trained",
            model_id=model_id,
        )
    )
    if pod_id in ctx._pods:
        ctx._pods[pod_id]["model_id"] = model_id
    return {"ok": True, "run_id": run_id, "model_id": model_id}


def run_benchmark_eval(ctx: ToolContext, pod_id: str = "", model_id: str = "", **_: Any) -> dict:
    # Simulated scores slightly around baseline
    scores = {
        "campaign_strategy": 0.74,
        "budget_allocation": 0.70,
        "creator_selection": 0.71,
        "aggregate": 0.716,
    }
    return {"scores": scores, "model_id": model_id, "pod_id": pod_id}


def teardown_pod(ctx: ToolContext, pod_id: str = "", **_: Any) -> dict:
    ctx._pods.pop(pod_id, None)
    return {"ok": True, "pod_id": pod_id, "status": "terminated"}


def get_baseline_scores(ctx: ToolContext, **_: Any) -> dict:
    row = (
        ctx.db.query(m.ModelRecord)
        .filter_by(tenant_id=ctx.tenant_id, is_production=True)
        .first()
    )
    if not row or not row.eval_scores_json:
        return {"scores": {"aggregate": 0.7}}
    return {"scores": json.loads(row.eval_scores_json), "model_id": row.id}


def compare_scores(ctx: ToolContext, new_scores: dict | None = None, **_: Any) -> dict:
    base = get_baseline_scores(ctx)["scores"]
    new_scores = new_scores or {}
    tol = ctx.settings.promotion_regression_tolerance
    regressions = []
    for k, v in base.items():
        if k == "aggregate":
            continue
        if k in new_scores and new_scores[k] < v - tol:
            regressions.append({"topic": k, "baseline": v, "new": new_scores[k]})
    agg_ok = new_scores.get("aggregate", 0) >= base.get("aggregate", 0)
    promote = agg_ok and not regressions
    return {
        "promote": promote,
        "aggregate_ok": agg_ok,
        "regressions": regressions,
        "baseline": base,
        "new_scores": new_scores,
    }


def register_model(
    ctx: ToolContext,
    model_id: str = "",
    dataset_version: str = "",
    config: dict | None = None,
    scores: dict | None = None,
    git_commit: str = "local",
    promote: bool = False,
    decision_reasoning: str = "",
    **_: Any,
) -> dict:
    if promote:
        for r in (
            ctx.db.query(m.ModelRecord)
            .filter_by(tenant_id=ctx.tenant_id, is_production=True)
            .all()
        ):
            r.is_production = False
    ctx.db.add(
        m.ModelRecord(
            id=model_id or _uid("mdl_"),
            tenant_id=ctx.tenant_id,
            dataset_version=dataset_version,
            training_config_json=json.dumps(config or {}),
            eval_scores_json=json.dumps(scores or {}),
            git_commit=git_commit,
            is_production=promote,
        )
    )
    ctx.log(
        "register_model",
        decision_reasoning,
        {"model_id": model_id, "promote": promote, "scores": scores},
    )
    return {"ok": True, "model_id": model_id, "promoted": promote}


def get_success_metrics(ctx: ToolContext, **_: Any) -> dict:
    camps = ctx.db.query(m.Campaign).filter_by(tenant_id=ctx.tenant_id).all()
    by_status: dict[str, int] = {}
    for c in camps:
        by_status[c.verification_status] = by_status.get(c.verification_status, 0) + 1
    cats = ctx.db.query(m.CategoryState).filter_by(tenant_id=ctx.tenant_id).all()
    tenant = ctx.db.query(m.Tenant).filter_by(id=ctx.tenant_id).first()
    return {
        "campaigns_by_status": by_status,
        "categories": [
            {
                "name": c.name,
                "verified": c.verified_count,
                "target": c.phase_target,
                "weeks_missed": c.weeks_missed_target,
            }
            for c in cats
        ],
        "open_escalations": ctx.db.query(m.Escalation)
        .filter_by(tenant_id=ctx.tenant_id, status="open")
        .count(),
        "open_contradictions": ctx.db.query(m.Contradiction)
        .filter_by(tenant_id=ctx.tenant_id, resolution_status="open")
        .count(),
        "budget": {
            "monthly_usd": tenant.monthly_budget_usd if tenant else 0,
            "spent_usd": tenant.spent_usd if tenant else 0,
        },
    }


def get_agent_health(ctx: ToolContext, **_: Any) -> dict:
    rows = ctx.db.query(m.AgentHealth).filter_by(tenant_id=ctx.tenant_id).all()
    return {
        "agents": [
            {
                "agent": r.agent,
                "last_run_at": r.last_run_at.isoformat() if r.last_run_at else None,
                "last_status": r.last_status,
                "run_count": r.run_count,
                "error_count": r.error_count,
                "estimated_cost_usd": r.estimated_cost_usd,
            }
            for r in rows
        ]
    }


def get_unified_escalation_queue(ctx: ToolContext, **_: Any) -> dict:
    rows = (
        ctx.db.query(m.Escalation)
        .filter_by(tenant_id=ctx.tenant_id, status="open")
        .order_by(m.Escalation.created_at.desc())
        .all()
    )
    return {
        "escalations": [
            {
                "id": r.id,
                "source_agent": r.source_agent,
                "kind": r.kind,
                "payload": json.loads(r.payload_json or "{}"),
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }


def route_human_decision(
    ctx: ToolContext, escalation_id: str = "", decision: str = "", **_: Any
) -> dict:
    row = (
        ctx.db.query(m.Escalation)
        .filter_by(id=escalation_id, tenant_id=ctx.tenant_id)
        .first()
    )
    if not row:
        return {"ok": False, "error": "not found"}
    row.status = "resolved"
    row.human_decision = decision
    row.resolved_at = _now()
    return {"ok": True, "id": row.id}


def generate_digest(ctx: ToolContext, **_: Any) -> dict:
    metrics = get_success_metrics(ctx)
    health = get_agent_health(ctx)
    esc = get_unified_escalation_queue(ctx)
    content = {
        "metrics": metrics,
        "agent_runs": health,
        "open_escalations": len(esc["escalations"]),
        "generated_at": _now().isoformat(),
    }
    return {"digest": content}


def write_digest_history(ctx: ToolContext, content: str = "", **_: Any) -> dict:
    did = _uid("dg_")
    if not isinstance(content, str):
        content = json.dumps(content)
    ctx.db.add(m.Digest(id=did, tenant_id=ctx.tenant_id, content=content))
    return {"ok": True, "id": did}


def get_active_contract(ctx: ToolContext, **_: Any) -> dict:
    row = (
        ctx.db.query(m.Contract)
        .filter_by(tenant_id=ctx.tenant_id, active=True)
        .order_by(m.Contract.version.desc())
        .first()
    )
    if not row:
        return {"contract": None}
    return {
        "contract": {
            "id": row.id,
            "client_name": row.client_name,
            "allowed_categories": json.loads(row.allowed_categories_json),
            "excluded_competitors": json.loads(row.excluded_competitors_json),
            "allowed_sources": json.loads(row.allowed_sources_json),
            "brand_voice": row.brand_voice,
            "version": row.version,
        }
    }


def validate_action(
    ctx: ToolContext,
    category: str = "",
    source: str = "",
    brand: str = "",
    competitor: str = "",
    query: str = "",
    **_: Any,
) -> dict:
    contract = get_active_contract(ctx).get("contract")
    if not contract:
        return {"result": "block", "reason": "no active contract"}
    reasons = []
    if category and category not in contract["allowed_categories"]:
        reasons.append(f"category '{category}' not allowed")
    if source and source not in contract["allowed_sources"]:
        reasons.append(f"source '{source}' not allowed")
    excluded = {x.lower() for x in contract["excluded_competitors"]}
    if competitor and competitor.lower() in excluded:
        reasons.append(f"competitor '{competitor}' excluded")
    if brand and brand.lower() in excluded:
        reasons.append(f"brand '{brand}' is excluded competitor")
    if query and any(x in query.lower() for x in excluded):
        reasons.append("query references excluded competitor")
    result = "block" if reasons else "approve"
    fid = _uid("sf_")
    ctx.db.add(
        m.ScopeFlag(
            id=fid,
            tenant_id=ctx.tenant_id,
            action_json=json.dumps(
                {
                    "category": category,
                    "source": source,
                    "brand": brand,
                    "competitor": competitor,
                    "query": query,
                }
            ),
            result=result,
            reason="; ".join(reasons) if reasons else "within contract",
        )
    )
    return {"result": result, "reasons": reasons, "flag_id": fid}


def flag_ambiguous_action(
    ctx: ToolContext, action: dict | None = None, reason: str = "", **_: Any
) -> dict:
    return create_escalation(
        ctx, kind="scope_ambiguous", message=reason, payload=action or {}
    )


def flag_repeated_violation(
    ctx: ToolContext, pattern: str = "", count: int = 0, **_: Any
) -> dict:
    return create_escalation(
        ctx,
        kind="repeated_scope_violation",
        message=f"Pattern '{pattern}' blocked {count} times",
        payload={"pattern": pattern, "count": count},
    )


def log_event(
    ctx: ToolContext,
    event_type: str = "",
    message: str = "",
    payload: dict | None = None,
    **_: Any,
) -> dict:
    eid = ctx.log(event_type, message, payload)
    return {"ok": True, "event_id": eid}


# Explicit registry — first parameter must be ToolContext
HANDLERS: dict[str, Callable[..., dict]] = {
    "get_research_brief": get_research_brief,
    "list_topic_schemas": list_topic_schemas,
    "score_all_active": score_all_active,
    "write_work_queue": write_work_queue,
    "get_open_contradictions": get_open_contradictions,
    "apply_auto_resolution": apply_auto_resolution,
    "create_escalation": create_escalation,
    "write_research_journal": write_research_journal,
    "get_current_assignment": get_current_assignment,
    "check_recent_searches": check_recent_searches,
    "trigger_discovery": trigger_discovery,
    "get_discovery_results": get_discovery_results,
    "score_relevance": score_relevance,
    "write_discovery_candidate": write_discovery_candidate,
    "record_search": record_search,
    "claim_candidate": claim_candidate,
    "collect_full_evidence": collect_full_evidence,
    "assess_completeness": assess_completeness,
    "write_to_raw_lake": write_to_raw_lake,
    "compute_preliminary_confidence": compute_preliminary_confidence,
    "extract_lightweight_signals": extract_lightweight_signals,
    "write_evidence_staging": write_evidence_staging,
    "discard_candidate": discard_candidate,
    "get_pending_dedup_batch": get_pending_dedup_batch,
    "compute_content_similarity": compute_content_similarity,
    "get_campaign_identity_signals": get_campaign_identity_signals,
    "compute_match_score": compute_match_score,
    "attach_evidence_to_campaign": attach_evidence_to_campaign,
    "create_campaign_stub": create_campaign_stub,
    "flag_ambiguous_match": flag_ambiguous_match,
    "get_pending_verification_batch": get_pending_verification_batch,
    "get_source_reliability": get_source_reliability,
    "check_phyllo_profile_consistency": check_phyllo_profile_consistency,
    "get_ambiguous_match_flag": get_ambiguous_match_flag,
    "resolve_ambiguous_match": resolve_ambiguous_match,
    "escalate_ambiguous_match": escalate_ambiguous_match,
    "update_verification_status": update_verification_status,
    "get_unextracted_verified_campaigns": get_unextracted_verified_campaigns,
    "get_campaign_evidence_content": get_campaign_evidence_content,
    "get_ontology": get_ontology,
    "extract_entities": extract_entities,
    "extract_relationships": extract_relationships,
    "score_extraction_confidence": score_extraction_confidence,
    "write_candidate_fact": write_candidate_fact,
    "flag_ontology_gap": flag_ontology_gap,
    "get_pending_graph_writes": get_pending_graph_writes,
    "query_existing_facts": query_existing_facts,
    "check_conflict": check_conflict,
    "write_graph_fact": write_graph_fact,
    "write_contradiction": write_contradiction,
    "reject_candidate_fact": reject_candidate_fact,
    "get_analysis_candidates": get_analysis_candidates,
    "get_cluster_campaigns": get_cluster_campaigns,
    "get_research_memory": get_research_memory,
    "write_strategic_analysis": write_strategic_analysis,
    "write_research_memory_summary": write_research_memory_summary,
    "get_seed_material": get_seed_material,
    "get_topic_schema": get_topic_schema,
    "generate_variation": generate_variation,
    "validate_business_logic_invariant": validate_business_logic_invariant,
    "write_draft_training_example": write_draft_training_example,
    "get_pending_review_batch": get_pending_review_batch,
    "construct_counter_argument": construct_counter_argument,
    "reassess_difficulty": reassess_difficulty,
    "check_negative_example_validity": check_negative_example_validity,
    "fact_check_against_graph": fact_check_against_graph,
    "update_review_status": update_review_status,
    "get_approved_examples_pool": get_approved_examples_pool,
    "get_current_benchmark_composition": get_current_benchmark_composition,
    "claim_for_benchmark": claim_for_benchmark,
    "get_prior_benchmark_versions": get_prior_benchmark_versions,
    "create_benchmark_version": create_benchmark_version,
    "flag_thin_coverage": flag_thin_coverage,
    "get_approved_non_benchmark_examples": get_approved_non_benchmark_examples,
    "check_semantic_duplicates": check_semantic_duplicates,
    "assign_splits": assign_splits,
    "write_training_examples_batch": write_training_examples_batch,
    "create_dataset_version": create_dataset_version,
    "write_manifest": write_manifest,
    "flag_undersized_split": flag_undersized_split,
    "get_training_run_config": get_training_run_config,
    "provision_training_pod": provision_training_pod,
    "run_training_job": run_training_job,
    "run_benchmark_eval": run_benchmark_eval,
    "teardown_pod": teardown_pod,
    "get_baseline_scores": get_baseline_scores,
    "compare_scores": compare_scores,
    "register_model": register_model,
    "get_success_metrics": get_success_metrics,
    "get_agent_health": get_agent_health,
    "get_unified_escalation_queue": get_unified_escalation_queue,
    "route_human_decision": route_human_decision,
    "generate_digest": generate_digest,
    "write_digest_history": write_digest_history,
    "get_active_contract": get_active_contract,
    "validate_action": validate_action,
    "flag_ambiguous_action": flag_ambiguous_action,
    "flag_repeated_violation": flag_repeated_violation,
    "log_event": log_event,
}
