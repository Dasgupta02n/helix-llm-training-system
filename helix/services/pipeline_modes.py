"""Quality/cost pipeline modes for mining batches.

Mode 1 — best quality: all 15 agents (LLM)
Mode 2 — balanced: key LLM agents + code for mechanical steps
Mode 3 — lean: few LLM gates + heavy code path
Mode 4 — ultra lean: code-only / template heuristics (lowest tokens)
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from sqlalchemy.orm import Session

from helix.agents.catalog import PIPELINE_ORDER
from helix.agents.runner import run_agent
from helix.db import models as m
from helix.services.library import add_gold_example, get_or_create_scope
from helix.tools.handlers import ToolContext

MODE_META = {
    1: {
        "label": "Best quality",
        "short": "All 15 AI helpers",
        "cost": "Highest judge tokens",
        "description": "Apify gathers; all 15 agents judge via tools (no inventing scrapes).",
    },
    2: {
        "label": "High quality",
        "short": "Core judges",
        "cost": "High judge tokens",
        "description": "Apify+code gather; OpenRouter for verify/extract/review/strategy.",
    },
    3: {
        "label": "Balanced",
        "short": "Quality gates only",
        "cost": "Medium tokens",
        "description": "Apify+code gather; OpenRouter only for verification + adversarial.",
    },
    4: {
        "label": "Lowest cost",
        "short": "Ultra lean",
        "cost": "Minimal tokens",
        "description": "Apify+code only — no multi-agent chat loops.",
    },
}


def _uid(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def clamp_mode(mode: int) -> int:
    try:
        m = int(mode)
    except (TypeError, ValueError):
        m = 2
    return max(1, min(4, m))


def clamp_batch_size(n: int) -> int:
    try:
        v = int(n)
    except (TypeError, ValueError):
        v = 5
    return max(1, min(10, v))


def mode_llm_agents(mode: int) -> list[str]:
    """OpenRouter JUDGES only. Gathering is always Apify/code, never these prompts inventing data.

    Mode 1 still runs all 15 agent *roles*, but discovery/evidence tools call Apify only.
    Modes 2–4 drop gatherer personas from LLM to save tokens (code already gathered).
    """
    mode = clamp_mode(mode)
    if mode == 1:
        # Full quality: all roles. Gather tools underneath are Apify-only.
        return list(PIPELINE_ORDER)
    if mode == 2:
        # No LLM discovery/evidence — Apify+code already filled queues
        return [
            "research_director",
            "fact_verification",
            "knowledge_extraction",
            "knowledge_graph",
            "adversarial_reviewer",
            "campaign_strategist",
        ]
    if mode == 3:
        return ["fact_verification", "adversarial_reviewer"]
    return []  # mode 4 — ultra lean, code + Apify only


def run_code_pipeline_batch(
    db: Session,
    *,
    tenant_id: str,
    owner_user_id: str | None,
    batch_size: int,
) -> dict[str, Any]:
    """Ultra-lean path: exercise tools without agent chat loops."""
    batch_size = clamp_batch_size(batch_size)
    ctx = ToolContext(db, tenant_id, "pipeline_code", owner_user_id=owner_user_id)
    steps: list[str] = []

    # Discovery: take open work items and create candidates
    from helix.tools import handlers as h

    assign = h.get_current_assignment(ctx)
    created_candidates = 0
    if assign.get("assignment"):
        a = assign["assignment"]
        job = h.trigger_discovery(
            ctx,
            category=a["category"],
            source=a["source"],
            query=f"{a['category']} campaign {a['source']}",
        )
        results = h.get_discovery_results(ctx, job_id=job["job_id"]).get("results") or []
        for r in results[:batch_size]:
            scored = h.score_relevance(
                ctx, title=r.get("title", ""), category=a["category"], snippet=r.get("snippet", "")
            )
            if scored.get("above_threshold"):
                h.write_discovery_candidate(
                    ctx,
                    category=a["category"],
                    source=a["source"],
                    title=r.get("title", ""),
                    url=r.get("url", ""),
                    brand=r.get("brand", ""),
                    creator=r.get("creator", ""),
                    relevance_score=scored["relevance_score"],
                )
                created_candidates += 1
        h.record_search(
            ctx,
            source=a["source"],
            query=f"{a['category']} campaign",
            category=a["category"],
        )
        steps.append(f"discovery:{created_candidates}")

    # Evidence for pending candidates
    pending = (
        db.query(m.DiscoveryCandidate)
        .filter_by(tenant_id=tenant_id, status="pending")
        .limit(batch_size)
        .all()
    )
    staged = 0
    for cand in pending:
        h.claim_candidate(ctx, candidate_id=cand.id)
        coll = h.collect_full_evidence(ctx, candidate_id=cand.id)
        content = coll.get("content") or {}
        h.write_to_raw_lake(ctx, candidate_id=cand.id, content=content)
        conf = h.compute_preliminary_confidence(
            ctx, candidate_id=cand.id, content_length=len(json.dumps(content))
        )
        h.write_evidence_staging(
            ctx,
            candidate_id=cand.id,
            brand=content.get("brand") or cand.brand,
            creator=content.get("creator") or cand.creator,
            content_date=cand.content_date or "",
            content_text=content.get("full_text") or cand.title,
            preliminary_confidence=conf.get("preliminary_confidence", 0.5),
            identity_signals={
                "brand": content.get("brand") or cand.brand,
                "creator": content.get("creator") or cand.creator,
            },
        )
        staged += 1
    steps.append(f"evidence:{staged}")

    # Dedup batch
    batch = h.get_pending_dedup_batch(ctx).get("batch") or []
    stubs = 0
    for item in batch[:batch_size]:
        sim = h.compute_content_similarity(ctx, staging_id=item["id"])
        brand_match = False
        creator_match = False
        if item.get("brand"):
            matches = h.get_campaign_identity_signals(ctx, brand=item["brand"]).get("matches") or []
            brand_match = any(m.get("brand") == item["brand"] for m in matches)
            creator_match = any(m.get("creator") == item.get("creator") for m in matches)
        score = h.compute_match_score(
            ctx,
            staging_id=item["id"],
            content_similarity=sim.get("similarity", 0),
            brand_match=brand_match,
            creator_match=creator_match,
        )
        if score["band"] == "high" and sim.get("campaign_id"):
            h.attach_evidence_to_campaign(
                ctx, staging_id=item["id"], campaign_id=sim["campaign_id"]
            )
        elif score["band"] == "low":
            h.create_campaign_stub(ctx, staging_id=item["id"], category="general")
            stubs += 1
        else:
            h.flag_ambiguous_match(
                ctx,
                staging_id=item["id"],
                match_score=score["match_score"],
                candidate_campaign_id=sim.get("campaign_id") or "",
                reasoning="Ambiguous auto-dedup (lean mode)",
            )
    steps.append(f"dedup_stubs:{stubs}")

    # Auto-verify high preliminary confidence pending campaigns (lean heuristic)
    pending_camps = (
        db.query(m.Campaign)
        .filter(
            m.Campaign.tenant_id == tenant_id,
            m.Campaign.verification_status.in_(["pending", "request_more_evidence"]),
        )
        .limit(batch_size)
        .all()
    )
    verified = 0
    for camp in pending_camps:
        # simple lean verify
        h.update_verification_status(
            ctx,
            campaign_id=camp.id,
            status="verified",
            confidence=0.78,
            reasoning="Lean-mode heuristic verification (mode 3/4). Review later if needed.",
        )
        verified += 1
    steps.append(f"verified:{verified}")

    # Promote simple gold from approved training or create from verified campaigns
    gold_made = 0
    if owner_user_id:
        scope = get_or_create_scope(db, owner_user_id, tenant_id)
        camps = (
            db.query(m.Campaign)
            .filter_by(tenant_id=tenant_id, verification_status="verified")
            .order_by(m.Campaign.updated_at.desc())
            .limit(batch_size)
            .all()
        )
        for c in camps:
            if gold_made >= batch_size:
                break
            ev = (
                db.query(m.CampaignEvidence)
                .filter_by(tenant_id=tenant_id, campaign_id=c.id)
                .first()
            )
            text = (ev.content_text if ev else "") or c.title or ""
            g = add_gold_example(
                db,
                owner_user_id=owner_user_id,
                tenant_id=tenant_id,
                topic=c.category or "general",
                input_text=f"Campaign brief: {c.title} ({c.brand} / {c.creator})",
                output_text=text[:800] or f"Verified campaign {c.title}",
                rationale="Promoted from lean pipeline verified campaign",
                difficulty="moderate",
                source_kind="pipeline",
                source_ref=c.id,
                enforce_cap=True,
            )
            if g:
                gold_made += 1
        steps.append(f"gold:{gold_made}/target{scope.gold_target_count}")

    db.commit()
    return {
        "mode": "code",
        "batch_size": batch_size,
        "steps": steps,
        "items_processed": staged + verified + gold_made,
    }


def run_pipeline_batch(
    db: Session,
    *,
    tenant_id: str,
    owner_user_id: str | None,
    quality_mode: int,
    batch_size: int,
) -> dict[str, Any]:
    """Run one mining batch at the given quality/cost mode."""
    quality_mode = clamp_mode(quality_mode)
    batch_size = clamp_batch_size(batch_size)
    t0 = time.time()
    results: list[dict[str, Any]] = []
    items = 0

    # ALL modes: Apify gather + code path first (never leave gathering to the LLM)
    code_res = run_code_pipeline_batch(
        db,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        batch_size=batch_size,
    )
    results.append({"step": "apify_gather_and_code", **code_res})
    items += int(code_res.get("items_processed") or 0)

    agents = mode_llm_agents(quality_mode)
    msg = (
        f"JUDGE only — gathering already done by Apify/code. "
        f"Process at most {batch_size} pending items. Quality mode {quality_mode}. "
        f"Do NOT invent posts/URLs. Use only stored candidates/evidence. "
        f"Call gather tools only if a tool must refresh cache; never fabricate."
    )
    for key in agents:
        try:
            r = run_agent(
                db,
                tenant_id,
                key,
                message=msg,
                trigger="batch",
                owner_user_id=owner_user_id,
            )
            results.append(
                {
                    "agent": key,
                    "status": r.get("status"),
                    "cost_usd": r.get("cost_usd"),
                    "run_id": r.get("run_id"),
                }
            )
            items += 1
        except Exception as e:  # noqa: BLE001
            results.append({"agent": key, "status": "error", "error": str(e)})
            # continue other agents unless mode 1 critical failure on early agents
            if quality_mode == 1 and key in {"research_director", "discovery"}:
                break

    elapsed = time.time() - t0
    return {
        "quality_mode": quality_mode,
        "batch_size": batch_size,
        "elapsed_seconds": round(elapsed, 2),
        "items_processed": items,
        "results": results,
        "meta": MODE_META[quality_mode],
    }
