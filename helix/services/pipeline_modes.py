"""Quality/cost pipeline modes for mining batches.

Mode 1 — best quality: all 15 agents (LLM)
Mode 2 — balanced: key LLM agents + code for mechanical steps
Mode 3 — lean: few LLM gates + heavy code path
Mode 4 — ultra lean: code-only / template heuristics (lowest tokens)

All modes are **domain-agnostic**: discovery queries and gold promotion follow
the tenant's active Research Brief, not a fixed influencer-marketing vertical.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

from sqlalchemy.orm import Session

from helix.agents.catalog import PIPELINE_ORDER
from helix.agents.runner import run_agent
from helix.db import models as m
from helix.services.brief import get_active_project, project_to_dict, sync_workspace_from_brief
from helix.services.gold_quality import synthesize_gold_pair
from helix.services.library import add_gold_example, get_or_create_scope
from helix.tools.handlers import ToolContext

MODE_META = {
    1: {
        "label": "Best quality",
        "short": "All 15 AI helpers",
        "cost": "Highest judge tokens",
        "description": "Web gather; all 15 agents judge via tools (no inventing scrapes).",
        "eta_batch_prior_sec": 180.0,
    },
    2: {
        "label": "High quality",
        "short": "Core judges",
        "cost": "High judge tokens",
        "description": "Web + code gather; model for verify/extract/review/strategy.",
        "eta_batch_prior_sec": 120.0,
    },
    3: {
        "label": "Balanced",
        "short": "Quality gates only",
        "cost": "Medium tokens",
        "description": "Web + code gather; model only for verification + adversarial.",
        "eta_batch_prior_sec": 75.0,
    },
    4: {
        "label": "Lowest cost",
        "short": "Ultra lean",
        "cost": "Minimal tokens",
        "description": "Web + code only — no multi-agent chat loops.",
        "eta_batch_prior_sec": 40.0,
    },
}

# Synthesis priors (used by batch_jobs / UI)
SYNTH_ETA_PRIOR = {1: 90.0, 2: 55.0, 3: 30.0, 4: 12.0}


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
    return max(1, min(100, v))


def mode_llm_agents(mode: int) -> list[str]:
    """OpenRouter JUDGES only. Gathering is always Apify/code."""
    mode = clamp_mode(mode)
    if mode == 1:
        return list(PIPELINE_ORDER)
    if mode == 2:
        return [
            "research_director",
            "fact_verification",
            "knowledge_extraction",
            "knowledge_graph",
            "training_quality_reviewer",
            "strategy_synthesizer",
        ]
    if mode == 3:
        return ["fact_verification", "training_quality_reviewer"]
    return []


def eta_prior_seconds(job_type: str, quality_mode: int) -> float:
    quality_mode = clamp_mode(quality_mode)
    if job_type == "synthesis":
        return SYNTH_ETA_PRIOR.get(quality_mode, 30.0)
    return float(MODE_META.get(quality_mode, {}).get("eta_batch_prior_sec") or 90.0)


def _brief_dict(db: Session, tenant_id: str) -> dict[str, Any]:
    p = get_active_project(db, tenant_id)
    return project_to_dict(p) if p else {}


def _search_query(brief: dict[str, Any], category: str, source: str) -> str:
    """Domain-agnostic search string from the research brief — never forces 'campaign'."""
    from helix.services.research_targets import build_search_queries

    qs = build_search_queries(brief, category=category, source=source, attempt=0, max_queries=1)
    return qs[0] if qs else (category or brief.get("domain") or "training examples")[:240]


def _primary_topic(brief: dict[str, Any], category: str) -> str:
    keys = brief.get("topic_keys") or []
    if isinstance(keys, list) and keys:
        return str(keys[0])
    cat = re.sub(r"[^a-z0-9]+", "_", (category or "general").lower()).strip("_")
    return cat or "general"


def _sample_pair(db: Session, tenant_id: str, topic: str) -> tuple[str, str, str]:
    schema = (
        db.query(m.TopicSchema)
        .filter_by(tenant_id=tenant_id, topic=topic, is_active=True)
        .first()
    )
    if schema and schema.sample_row_json:
        try:
            s = json.loads(schema.sample_row_json)
            return (
                str(s.get("input") or ""),
                str(s.get("output") or ""),
                str(s.get("rationale") or ""),
            )
        except json.JSONDecodeError:
            pass
    return ("", "", "")


def _already_promoted_refs(db: Session, owner_user_id: str, tenant_id: str) -> set[str]:
    """Source refs already written as *verified* gold (rejected must not block retries)."""
    rows = (
        db.query(m.GoldExample.source_ref)
        .filter_by(owner_user_id=owner_user_id, tenant_id=tenant_id, is_archived=False)
        .filter(m.GoldExample.source_ref.isnot(None))
        .filter(m.GoldExample.verification_status != "rejected")
        .all()
    )
    return {r[0] for r in rows if r[0]}


def gather_attempt_plan(batch_size: int) -> dict[str, Any]:
    """How many Apify runs a mining batch is allowed.

    Small jobs (≤10): at most two waits. Each attempt is ONE actor run that
    carries every query for that attempt. No 2-page deep scrape.
    """
    small = clamp_batch_size(batch_size) <= 10
    if small:
        return {
            "small": True,
            "max_attempts": 2,
            "queries_per_attempt": (2, 3),
            "deep": False,
            "force_refresh": False,
        }
    return {
        "small": False,
        "max_attempts": 3,
        "queries_per_attempt": (2, 3, 3),
        "deep": None,  # attempt >= 1
        "force_refresh": None,  # attempt > 0
    }


def run_code_pipeline_batch(
    db: Session,
    *,
    tenant_id: str,
    owner_user_id: str | None,
    batch_size: int,
    progress_cb: Any | None = None,
) -> dict[str, Any]:
    """Domain-agnostic code path: brief-driven discovery + gold promotion."""
    batch_size = clamp_batch_size(batch_size)
    ctx = ToolContext(db, tenant_id, "pipeline_code", owner_user_id=owner_user_id)
    steps: list[str] = []
    zero_evidence = False
    warnings: list[str] = []

    def _note(msg: str) -> None:
        if progress_cb:
            try:
                progress_cb(msg)
            except Exception:  # noqa: BLE001
                pass

    # Align category/source queues to the active research brief
    sync = sync_workspace_from_brief(db, tenant_id)
    steps.append(f"brief_sync:{sync.get('categories', 0)}c/{sync.get('queue', 0)}q")
    brief = _brief_dict(db, tenant_id)

    from helix.tools import handlers as h

    # Promote BYO corpus into candidates + full evidence + verified campaign stubs
    # *before* web discovery / agent judges, so they see real FAQ text not title scraps.
    try:
        from helix.services.corpus import promote_corpus_into_pipeline

        # Attach active project id so corpus is plan-scoped
        from helix.services.brief import get_active_project, project_to_dict as _p2d

        _proj = get_active_project(db, tenant_id)
        if _proj:
            brief = {**_p2d(_proj), **(brief or {})}
            brief["id"] = _proj.id
            brief["project_id"] = _proj.id
        early_corpus = promote_corpus_into_pipeline(
            db,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            brief=brief,
            batch_size=batch_size,
        )
        steps.append(
            f"corpus_early:docs={early_corpus.get('docs', 0)}/"
            f"cands+={early_corpus.get('candidates_created', 0)}/"
            f"units={len(early_corpus.get('units') or [])}"
        )
        if early_corpus.get("docs"):
            zero_evidence = False
    except Exception as e:  # noqa: BLE001
        warnings.append(f"corpus_early: {e}")
        steps.append("corpus_early:error")

    assign = h.get_current_assignment(ctx)
    if not assign.get("assignment"):
        # force queue from brief once more
        sync_workspace_from_brief(db, tenant_id, force_queue=True)
        assign = h.get_current_assignment(ctx)

    created_candidates = 0
    gather_results = 0
    apify_cost_usd = 0.0
    queries_tried: list[str] = []
    if assign.get("assignment"):
        from helix.services.research_targets import (
            build_search_queries,
            min_evidence_threshold,
            preferred_sources,
            research_domain_kind,
            score_item_for_kind,
        )

        a = assign["assignment"]
        kind = research_domain_kind(brief, a.get("category") or "")
        from helix.services.source_adapter import SOCIAL_CHANNELS, sources_for_gather

        gather_specs = sources_for_gather(
            brief_sources=list(brief.get("sources") or []),
            assignment_source=a.get("source"),
            domain_kind=kind,
            fallback=preferred_sources(kind),
        )
        unreachable_specs = [s for s in gather_specs if not s.get("reachable")]
        reachable_specs = [s for s in gather_specs if s.get("reachable")]
        if not reachable_specs:
            reachable_specs = sources_for_gather(
                brief_sources=[],
                assignment_source="web",
                domain_kind=kind,
                fallback=["web"],
            )
        # Social assignment must not override a plan that named education/docs/forums.
        if kind != "influencer" and any(
            s.get("channel") in SOCIAL_CHANNELS for s in reachable_specs
        ) and any(
            s.get("channel") not in SOCIAL_CHANNELS for s in reachable_specs
        ):
            reachable_specs = [
                s for s in reachable_specs if s.get("channel") not in SOCIAL_CHANNELS
            ] or reachable_specs

        if unreachable_specs:
            labels = ", ".join(s["label"] for s in unreachable_specs)
            reasons = "; ".join(
                f"{s['label']}: {s.get('reason') or 'not publicly searchable'}"
                for s in unreachable_specs
            )
            warnings.append(f"source_alignment: cannot reach {labels} — {reasons}")
            steps.append(f"source_unreachable:{len(unreachable_specs)}")
            h.create_escalation(
                ctx,
                kind="source_alignment",
                message=(
                    f"The plan names source type(s) we cannot gather from the public web: "
                    f"{reasons}. Attach those as corpus/materials, or change Plan → sources. "
                    f"Discovery will still search: "
                    + ", ".join(s["label"] for s in reachable_specs)
                    + "."
                ),
                payload={
                    "unreachable": [s["label"] for s in unreachable_specs],
                    "attempted": [s["label"] for s in reachable_specs],
                },
            )

        source = reachable_specs[0]["channel"]
        steps.append(
            "sources:"
            + ",".join(f"{s['label']}→{s['channel']}" for s in gather_specs[:6])
        )

        min_hits = min_evidence_threshold(batch_size)
        plan = gather_attempt_plan(batch_size)
        max_attempts = int(plan["max_attempts"])
        seen_urls: set[str] = set()
        all_results: list[dict[str, Any]] = []
        hits_by_label: dict[str, int] = {s["label"]: 0 for s in reachable_specs}

        for attempt in range(max_attempts):
            per = plan["queries_per_attempt"]
            nq = per[attempt] if attempt < len(per) else per[-1]
            deep = False if plan["deep"] is False else attempt >= 1
            force_refresh = (
                False if plan["force_refresh"] is False else attempt > 0
            )
            spec = reachable_specs[attempt % len(reachable_specs)]
            source = spec["channel"]
            qs = [
                q
                for q in build_search_queries(
                    brief,
                    category=a["category"],
                    source=source,
                    attempt=attempt,
                    max_queries=nq,
                    extra_operators=spec.get("operators") or [],
                    source_label=spec.get("label") or "",
                )
                if q not in queries_tried
            ]
            if not qs:
                continue
            queries_tried.extend(qs)
            _note(
                f"Gathering sources — Apify search {attempt + 1}/{max_attempts} "
                f"({len(qs)} quer{'y' if len(qs) == 1 else 'ies'} in one job)…"
            )
            job = h.trigger_discovery(
                ctx,
                category=a["category"],
                source=source,
                queries=qs,
                max_results=15 if deep else None,
                force_refresh=force_refresh,
                deep=deep,
                domain_kind=kind,
            )
            attempt_hits = 0
            if not job.get("ok"):
                warnings.append(
                    job.get("error") or job.get("message") or "Gather failed"
                )
            else:
                apify_cost_usd += float(job.get("apify_cost_usd") or 0.0)
                for r in job.get("results") or []:
                    url = (r.get("url") or "").strip()
                    key = url or (r.get("title") or "")
                    if key and key in seen_urls:
                        continue
                    if key:
                        seen_urls.add(key)
                    kind_sc = score_item_for_kind(
                        kind=kind,
                        title=r.get("title") or "",
                        snippet=r.get("snippet") or "",
                        url=url,
                        category=a["category"],
                        query=" ".join(qs)[:240],
                    )
                    if kind == "support" and kind_sc.get("ad_like") and not kind_sc.get(
                        "help_like"
                    ):
                        continue
                    r = {**r, "_kind_score": kind_sc["relevance_score"], "_query": qs[0]}
                    all_results.append(r)
                    attempt_hits += 1
                    hits_by_label[spec["label"]] = hits_by_label.get(spec["label"], 0) + 1
                for query in qs:
                    h.record_search(
                        ctx, source=source, query=query, category=a["category"]
                    )
            gather_results = len(all_results)
            steps.append(
                f"gather_attempt{attempt}:q={len(qs)},runs=1,hits={attempt_hits},"
                f"total={gather_results},deep={deep}"
            )
            if gather_results >= min_hits:
                break
            if attempt + 1 < max_attempts:
                warnings.append(
                    f"Thin yield ({gather_results}<{min_hits}) — broadening research "
                    f"(attempt {attempt + 2}/{max_attempts})."
                )

        # Sort by domain-aware score and promote best
        all_results.sort(key=lambda x: float(x.get("_kind_score") or 0), reverse=True)
        gather_results = len(all_results)
        if gather_results == 0:
            zero_evidence = True
            warnings.append(
                f"No verifiable sources found for “{a['category']}” "
                f"after {len(queries_tried)} varied queries. 0 new candidates."
            )

        empty_named = [
            s["label"]
            for s in reachable_specs
            if hits_by_label.get(s["label"], 0) == 0
            and (s.get("label") or "").lower()
            not in {"web", "blog", "docs", s.get("channel", "")}
        ]
        if empty_named:
            warnings.append(
                "source_alignment: attempted but 0 hits for "
                + ", ".join(empty_named)
            )
            h.create_escalation(
                ctx,
                kind="source_alignment",
                message=(
                    "Discovery searched the public web for these plan source types "
                    f"and found nothing usable: {', '.join(empty_named)}. "
                    "This is not a silent fallback to Instagram/TikTok — those "
                    "were not used as a substitute. Attach corpus for those "
                    "sources, or rename Plan → sources to something we can query."
                ),
                payload={
                    "zero_yield": empty_named,
                    "hits_by_source": hits_by_label,
                    "queries_tried": queries_tried[:12],
                },
            )

        for r in all_results[: max(batch_size * 2, min_hits)]:
            scored = h.score_relevance(
                ctx,
                title=r.get("title", ""),
                category=a["category"],
                snippet=r.get("snippet", ""),
                url=r.get("url", ""),
                query=r.get("_query", ""),
                domain_kind=kind,
            )
            if brief.get("domain"):
                scored2 = h.score_relevance(
                    ctx,
                    title=r.get("title", ""),
                    category=str(brief.get("domain")),
                    snippet=r.get("snippet", ""),
                    url=r.get("url", ""),
                    domain_kind=kind,
                )
                if scored2.get("relevance_score", 0) > scored.get("relevance_score", 0):
                    scored = scored2
            if scored.get("above_threshold") or scored.get("needs_judgment"):
                h.write_discovery_candidate(
                    ctx,
                    category=a["category"],
                    source=source,
                    title=r.get("title", ""),
                    url=r.get("url", ""),
                    brand=r.get("brand", "") or brief.get("domain", ""),
                    creator=r.get("creator", ""),
                    relevance_score=scored.get("relevance_score", 0.5),
                    gather_item_id=r.get("gather_item_id") or r.get("id") or "",
                )
                created_candidates += 1
            if created_candidates >= batch_size:
                break
        steps.append(
            f"discovery:{created_candidates}/{gather_results}/queries={len(queries_tried)}/kind={kind}"
        )
    else:
        zero_evidence = True
        warnings.append("No discovery assignment — set categories/sources in Plan.")
        steps.append("discovery:no_assignment")

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
                "domain": brief.get("domain"),
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
            h.create_campaign_stub(
                ctx,
                staging_id=item["id"],
                category=item.get("category") or "general",
            )
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

    # Auto-verify high preliminary confidence pending records
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
        h.update_verification_status(
            ctx,
            campaign_id=camp.id,
            status="verified",
            confidence=0.72,
            reasoning=(
                "Code-path verification for domain-agnostic mining. "
                "Treat as provisional gold seed; adversarial review should re-check."
            ),
        )
        verified += 1
    steps.append(f"verified:{verified}")

    # Promote NEW gold from gathered evidence (not only influencer Campaign graph)
    gold_made = 0
    gold_skipped_dup = 0
    gold_rejected = 0
    corpus_gold_new = 0
    reject_log: list[dict] = []
    if owner_user_id:
        scope = get_or_create_scope(db, owner_user_id, tenant_id)
        promoted_refs = _already_promoted_refs(db, owner_user_id, tenant_id)
        domain = brief.get("domain") or "this domain"
        known_ids = {
            g.id
            for g in db.query(m.GoldExample.id)
            .filter_by(owner_user_id=owner_user_id, tenant_id=tenant_id, is_archived=False)
            .all()
        }

        tenant_row = db.query(m.Tenant).filter_by(id=tenant_id).first()

        def _bump_category_verified(category: str) -> None:
            """Keep dashboard verified counts aligned with gold we actually write."""
            if not category:
                return
            row = (
                db.query(m.CategoryState)
                .filter_by(tenant_id=tenant_id, name=category)
                .first()
            )
            if row:
                row.verified_count = int(row.verified_count or 0) + 1

        def _try_add_gold(
            *,
            source_ref: str,
            title: str,
            text: str,
            category: str,
            url: str = "",
            meta: dict | None = None,
            topic_override: str | None = None,
        ) -> None:
            nonlocal gold_made, gold_skipped_dup, gold_rejected
            if gold_made >= batch_size or not source_ref:
                return
            if source_ref in promoted_refs:
                gold_skipped_dup += 1
                return
            body = (text or title or "").strip()
            if len(body) < 15:
                return
            topic = topic_override or _primary_topic(brief, category or "general")
            # Prefer plan category slug as topic when it looks like a real category
            if category and category not in {"general", "web", "blog"}:
                topic = re.sub(r"[^a-z0-9]+", "_", category.lower()).strip("_") or topic
            # LLM synthesis when available; hard quality gates reject echo/refuse patterns
            pair = synthesize_gold_pair(
                brief=brief,
                title=title or topic,
                evidence=body,
                topic=topic,
                url=url,
                tenant=tenant_row,
                prefer_llm=True,
            )
            if not pair or not pair.get("quality_ok"):
                gold_rejected += 1
                reject_log.append(
                    {
                        "source_ref": source_ref,
                        "title": (title or "")[:80],
                        "reasons": (pair or {}).get("reject_reasons")
                        or ["quality_gate_or_synth_failed"],
                    }
                )
                return
            g = add_gold_example(
                db,
                owner_user_id=owner_user_id,
                tenant_id=tenant_id,
                topic=topic,
                input_text=pair["input"][:4000],
                output_text=pair["output"][:2000],
                rationale=(pair.get("rationale") or "")[:1000],
                difficulty=pair.get("difficulty") or "moderate",
                is_negative=bool(pair.get("is_negative")),
                source_kind=(
                    "corpus"
                    if (meta or {}).get("from") == "user_corpus"
                    else "pipeline"
                ),
                source_ref=source_ref,
                verification_status=pair.get("verification_status") or "verified",
                enforce_cap=True,
                metadata={
                    "title": title,
                    "category": category,
                    "domain": domain,
                    "run": "code_pipeline",
                    "synth": pair.get("synth"),
                    **(meta or {}),
                },
            )
            if not g:
                return
            if g.id in known_ids:
                gold_skipped_dup += 1
            else:
                known_ids.add(g.id)
                gold_made += 1
                promoted_refs.add(source_ref)
                _bump_category_verified(category)

        # 0) BYO corpus → candidates/evidence/campaigns, then dedicated gold write
        # (must not go through near-dup path that silently swallows support templates)
        corpus_gold_new = 0
        try:
            from helix.services.corpus import (
                promote_corpus_into_pipeline,
                write_corpus_units_as_gold,
            )

            from helix.services.brief import get_active_project, project_to_dict
            from helix.services.corpus import domain_relevance_score

            # Ensure brief carries project id for plan-scoped corpus
            proj = get_active_project(db, tenant_id)
            if proj:
                brief = {**project_to_dict(proj), **(brief or {})}
                brief["id"] = proj.id
                brief["project_id"] = proj.id

            promo = promote_corpus_into_pipeline(
                db,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                brief=brief,
                batch_size=batch_size,
            )
            corpus_units = promo.get("units") or []
            # Retry path: only [corpus] campaigns that match THIS plan's domain
            # (never re-inject another plan's food-delivery FAQ into HR mining).
            if not corpus_units:
                camps = (
                    db.query(m.Campaign)
                    .filter_by(tenant_id=tenant_id, verification_status="verified")
                    .order_by(m.Campaign.updated_at.desc())
                    .limit(batch_size * 6)
                    .all()
                )
                domain_l = (brief.get("domain") or "").lower()
                for c in camps:
                    if not (c.title or "").startswith("[corpus]"):
                        continue
                    # Brand is set to plan domain at campaign create time
                    brand_l = (c.brand or "").lower()
                    if domain_l and brand_l and domain_l not in brand_l and brand_l not in domain_l:
                        continue
                    ev = (
                        db.query(m.CampaignEvidence)
                        .filter_by(tenant_id=tenant_id, campaign_id=c.id)
                        .first()
                    )
                    body = ((ev.content_text if ev else "") or "").strip()
                    if len(body) < 40:
                        continue
                    if domain_relevance_score(f"{c.title or ''}\n{body}", brief) < 0.12:
                        continue
                    corpus_units.append(
                        {
                            "source_ref": f"corpus:camp:{c.id}"[:64],
                            "title": (c.title or "Corpus").replace("[corpus] ", "", 1),
                            "evidence": body,
                            "category": c.category or "general",
                            "url": f"corpus://campaign/{c.id}",
                            "corpus_id": c.id,
                            "candidate_id": None,
                            "project_id": brief.get("id"),
                        }
                    )

            gold_write = write_corpus_units_as_gold(
                db,
                tenant_id=tenant_id,
                owner_user_id=owner_user_id,
                brief=brief,
                units=corpus_units,
                batch_size=batch_size,
                tenant=tenant_row,
            )
            corpus_gold_new = int(gold_write.get("corpus_gold_new") or 0)
            gold_made += corpus_gold_new
            gold_skipped_dup += int(gold_write.get("corpus_gold_skipped") or 0)
            gold_rejected += int(gold_write.get("corpus_gold_rejected") or 0)
            for d in gold_write.get("details") or []:
                if d.get("status") == "rejected":
                    reject_log.append(
                        {
                            "source_ref": d.get("source_ref"),
                            "title": "",
                            "reasons": d.get("reasons") or ["corpus_synth_failed"],
                        }
                    )
            for gid in gold_write.get("created_ids") or []:
                known_ids.add(gid)
            steps.append(
                f"corpus:docs={promo.get('docs', 0)}/"
                f"cands+={promo.get('candidates_created', 0)}/"
                f"units={len(corpus_units)}/"
                f"gold={corpus_gold_new}/"
                f"skip={gold_write.get('corpus_gold_skipped', 0)}/"
                f"rej={gold_write.get('corpus_gold_rejected', 0)}"
            )
            if corpus_units and corpus_gold_new == 0:
                detail_bits = []
                for d in (gold_write.get("details") or [])[:5]:
                    st = d.get("status")
                    if st == "goal_cap_reached":
                        detail_bits.append(
                            f"{d.get('source_ref')}:goal_cap_reached"
                            f"(verified={d.get('verified_count')}/"
                            f"target={d.get('gold_target')})"
                        )
                    elif st == "write_returned_null":
                        detail_bits.append(
                            f"{d.get('source_ref')}:write_returned_null"
                        )
                    else:
                        detail_bits.append(
                            f"{d.get('source_ref')}:{st}"
                            f"{(':' + ','.join(d.get('reasons') or [])[:80]) if d.get('reasons') else ''}"
                        )
                warnings.append(
                    f"Corpus units={len(corpus_units)} but 0 GoldExample rows written "
                    f"(skipped={gold_write.get('corpus_gold_skipped')}, "
                    f"rejected={gold_write.get('corpus_gold_rejected')}, "
                    f"errors={gold_write.get('errors')}). "
                    f"details=[{'; '.join(detail_bits)}]"
                )
        except Exception as e:  # noqa: BLE001
            warnings.append(f"corpus: {e}")
            steps.append(f"corpus:error:{e}")

        # 1) Direct from discovery candidates (most reliable domain-agnostic path)
        from helix.services.corpus import domain_relevance_score as _dom_score

        cands = (
            db.query(m.DiscoveryCandidate)
            .filter_by(tenant_id=tenant_id)
            .order_by(m.DiscoveryCandidate.created_at.desc())
            .limit(batch_size * 6)
            .all()
        )
        for cand in cands:
            # Prefer staged evidence text if present
            staging = (
                db.query(m.EvidenceStaging)
                .filter_by(tenant_id=tenant_id, candidate_id=cand.id)
                .order_by(m.EvidenceStaging.created_at.desc())
                .first()
            )
            text = ""
            if staging and staging.content_text:
                text = staging.content_text
            else:
                text = " ".join(
                    p
                    for p in [cand.title or "", getattr(cand, "snippet", "") or ""]
                    if p
                )
            # pull snippet from raw if available
            if len(text) < 40 and cand.url:
                text = f"{cand.title or ''}\nSource: {cand.url}"
            # Never promote another plan's corpus candidates into this plan's gold
            is_corpus_cand = (cand.source or "") == "corpus" or (
                cand.url or ""
            ).startswith("corpus://")
            if is_corpus_cand:
                # Corpus gold is written only via write_corpus_units_as_gold (plan-scoped)
                continue
            _try_add_gold(
                source_ref=f"cand:{cand.id}",
                title=cand.title or cand.category or "source",
                text=text,
                category=cand.category or "general",
                url=cand.url or "",
                meta={"from": "discovery_candidate"},
            )
            if gold_made >= batch_size:
                break

        # 2) Also promote newly verified campaign stubs (legacy path)
        if gold_made < batch_size:
            camps = (
                db.query(m.Campaign)
                .filter_by(tenant_id=tenant_id, verification_status="verified")
                .order_by(m.Campaign.updated_at.desc())
                .limit(batch_size * 3)
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
                text = ((ev.content_text if ev else "") or c.title or "").strip()
                _try_add_gold(
                    source_ref=c.id,
                    title=c.title or "record",
                    text=text,
                    category=c.category or "general",
                    meta={"from": "campaign"},
                )

        steps.append(
            f"gold_new:{gold_made}/skipped_dup:{gold_skipped_dup}/"
            f"quality_rejected:{gold_rejected}/target:{scope.gold_target_count}"
        )
        if reject_log:
            steps.append(
                "reject_sample:"
                + ";".join(
                    f"{r['source_ref']}:{','.join(r['reasons'][:3])}"
                    for r in reject_log[:5]
                )
            )

    if gold_made == 0 and (zero_evidence or gather_results == 0):
        zero_evidence = True
        if not any("No verifiable" in w for w in warnings):
            warnings.append(
                "No new gold examples created this batch — 0 on-topic verifiable sources "
                "or all candidates already in your library."
            )

    role_blob = f"{brief.get('mission') or ''} {brief.get('domain') or ''} {brief.get('agent_instructions') or ''}"
    role_text = brief.get("domain") or brief.get("mission") or ""
    risk = "medium"
    instr = str(brief.get("agent_instructions") or "")
    if "ROLE:" in instr:
        role_text = instr.split("ROLE:", 1)[1].split("\n", 1)[0].strip() or role_text
    if "RISK:" in instr:
        risk = instr.split("RISK:", 1)[1].split("\n", 1)[0].strip() or risk
    try:
        from helix.services.gold_quality import reverify_gold_for_role

        rv = reverify_gold_for_role(
            db,
            tenant_id=tenant_id,
            owner_user_id=owner_user_id,
            role_text=role_text or role_blob,
            risk_level=risk,
        )
        if rv.get("newly_rejected"):
            gold_made = max(0, gold_made - int(rv["newly_rejected"]))
            gold_rejected += int(rv["newly_rejected"])
            steps.append(
                f"role_reverify:rejected={rv['newly_rejected']}/scanned={rv['scanned']}"
            )
    except Exception as e:  # noqa: BLE001
        warnings.append(f"role_reverify: {e}")

    db.commit()
    return {
        "mode": "code",
        "batch_size": batch_size,
        "steps": steps,
        "items_processed": staged + verified + gold_made,
        "gold_new": gold_made,
        "gold_rejected": gold_rejected,
        "gold_reject_sample": reject_log[:8] if owner_user_id else [],
        "corpus_gold_new": corpus_gold_new if owner_user_id else 0,
        "candidates_new": created_candidates,
        "gather_results": gather_results,
        "apify_cost_usd": round(apify_cost_usd, 6),
        "zero_evidence": zero_evidence,
        "warnings": warnings,
        "domain": brief.get("domain") or "",
        "mission_snippet": (brief.get("mission") or "")[:120],
    }


def _user_gold_count(db: Session, owner_user_id: str | None, tenant_id: str) -> int:
    if not owner_user_id:
        return 0
    return (
        db.query(m.GoldExample)
        .filter_by(
            owner_user_id=owner_user_id,
            tenant_id=tenant_id,
            is_archived=False,
        )
        .count()
    )


def run_pipeline_batch(
    db: Session,
    *,
    tenant_id: str,
    owner_user_id: str | None,
    quality_mode: int,
    batch_size: int,
    progress_cb: Any | None = None,
) -> dict[str, Any]:
    """Run one mining batch at the given quality/cost mode."""
    quality_mode = clamp_mode(quality_mode)
    batch_size = clamp_batch_size(batch_size)
    t0 = time.time()
    results: list[dict[str, Any]] = []
    items = 0
    warnings: list[str] = []
    zero_evidence = False

    def _progress(msg: str) -> None:
        if progress_cb:
            try:
                progress_cb(msg)
            except Exception:  # noqa: BLE001
                pass

    gold_before = _user_gold_count(db, owner_user_id, tenant_id)
    tenant_row = db.query(m.Tenant).filter_by(id=tenant_id).first()
    or_before = float(getattr(tenant_row, "openrouter_spent_usd", 0.0) or 0.0) if tenant_row else 0.0
    ap_before = float(getattr(tenant_row, "apify_spent_usd", 0.0) or 0.0) if tenant_row else 0.0

    _progress("Gathering sources from your research plan…")
    code_res = run_code_pipeline_batch(
        db,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        batch_size=batch_size,
        progress_cb=progress_cb,
    )
    results.append({"step": "apify_gather_and_code", **code_res})
    items += int(code_res.get("items_processed") or 0)
    zero_evidence = bool(code_res.get("zero_evidence"))
    warnings.extend(code_res.get("warnings") or [])
    _progress(
        f"Gather finished — {items} item(s)"
        + ("; no new evidence this pass" if zero_evidence else "")
        + "."
    )

    agents = mode_llm_agents(quality_mode)
    brief = _brief_dict(db, tenant_id)
    domain = brief.get("domain") or "the active research domain"
    msg = (
        f"JUDGE only — gathering already done. "
        f"Active domain: {domain}. "
        f"Process at most {batch_size} pending items. Quality mode {quality_mode}. "
        f"Obey the Research Brief. Do NOT invent posts/URLs or domain facts. "
        f"For customer-support domains: ideal replies must help the end customer "
        f"(friendly tone, concrete next step). Never demand internal docs from them. "
        f"Never approve outputs that only paste raw scraped marketing text. "
        f"Faithfulness: if input describes missing/blank fields, acknowledge them."
    )
    for key in agents:
        _progress(f"Running helper: {key}…")
        try:
            r = run_agent(
                db,
                tenant_id,
                key,
                message=msg,
                trigger="batch",
                owner_user_id=owner_user_id,
            )
            agent_cost = float(r.get("cost_usd") or r.get("openrouter_cost_usd") or 0.0)
            results.append(
                {
                    "agent": key,
                    "status": r.get("status"),
                    "cost_usd": agent_cost,
                    "cost_source": r.get("cost_source"),
                    "run_id": r.get("run_id"),
                }
            )
            items += 1
            _progress(
                f"Helper {key} finished ({r.get('status') or 'ok'})"
                + (f" · ${agent_cost:.4f}" if agent_cost else "")
            )
        except Exception as e:  # noqa: BLE001
            results.append({"agent": key, "status": "error", "error": str(e)})
            warnings.append(f"{key}: {e}")
            if quality_mode == 1 and key in {"research_director", "discovery"}:
                break

    # Authoritative count: actual library delta (includes agent-promoted gold too)
    gold_after = _user_gold_count(db, owner_user_id, tenant_id)
    gold_new = max(0, gold_after - gold_before)
    _progress(f"Library check — {gold_new} new gold this batch (total in account {gold_after}).")
    # Prefer DB delta; fall back to code path counter if owner missing
    if owner_user_id is None:
        gold_new = int(code_res.get("gold_new") or 0)

    # Re-read tenant spend deltas so gold_quality LLM synth + agents both count
    db.refresh(tenant_row) if tenant_row else None
    or_after = float(getattr(tenant_row, "openrouter_spent_usd", 0.0) or 0.0) if tenant_row else 0.0
    ap_after = float(getattr(tenant_row, "apify_spent_usd", 0.0) or 0.0) if tenant_row else 0.0
    openrouter_cost_usd = max(0.0, or_after - or_before)
    apify_cost_usd = max(0.0, ap_after - ap_before)
    # Fallback if split counters missing on old rows mid-migration
    if openrouter_cost_usd == 0.0 and apify_cost_usd == 0.0:
        apify_cost_usd = float(code_res.get("apify_cost_usd") or 0.0)
        openrouter_cost_usd = sum(
            float(r.get("cost_usd") or 0.0)
            for r in results
            if r.get("agent")
        )

    elapsed = time.time() - t0
    total_cost = openrouter_cost_usd + apify_cost_usd
    if gold_new == 0 and zero_evidence and int(code_res.get("gather_results") or 0) == 0:
        user_message = (
            "No verifiable sources found for this topic — 0 new gold examples. "
            "Old library items (if any) were not produced by this run."
        )
    elif gold_new == 0:
        rejected = int(code_res.get("gold_rejected") or 0)
        user_message = (
            f"Batch finished with 0 new gold examples "
            f"(candidates={code_res.get('candidates_new', 0)}, "
            f"gather_hits={code_res.get('gather_results', 0)}"
            f"{f', quality_rejected={rejected}' if rejected else ''}). "
            "Existing library rows are unchanged."
        )
    else:
        user_message = (
            f"Batch added {gold_new} new gold example(s) "
            f"(candidates={code_res.get('candidates_new', 0)}, "
            f"gather_hits={code_res.get('gather_results', 0)})."
        )
    from helix.services.cost_tracking import user_charge_usd

    user_message += (
        f" Usage ${user_charge_usd(total_cost):.4f}."
    )

    return {
        "quality_mode": quality_mode,
        "batch_size": batch_size,
        "elapsed_seconds": round(elapsed, 2),
        "items_processed": items,
        "gold_new": gold_new,
        "gold_before": gold_before,
        "gold_after": gold_after,
        "code_gold_new": int(code_res.get("gold_new") or 0),
        "zero_evidence": zero_evidence and gold_new == 0,
        "warnings": warnings,
        "user_message": user_message,
        "results": results,
        "meta": MODE_META[quality_mode],
        "openrouter_cost_usd": round(openrouter_cost_usd, 6),
        "apify_cost_usd": round(apify_cost_usd, 6),
        "cost_usd": round(total_cost, 6),
    }
