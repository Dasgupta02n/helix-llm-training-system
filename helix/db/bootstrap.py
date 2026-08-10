"""First-boot bootstrap: superadmin, demo tenant, seed data."""

from __future__ import annotations

import json
import uuid

from sqlalchemy.orm import Session

from helix.config import CATEGORIES, get_settings
from helix.db import models as m
from helix.security import hash_password


def _uid(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


AGENT_KEYS = [
    "research_director",
    "discovery",
    "evidence_collector",
    "duplicate_resolver",
    "fact_verification",
    "knowledge_extraction",
    "knowledge_graph",
    "campaign_strategist",
    "synthetic_generator",
    "adversarial_reviewer",
    "benchmark_builder",
    "dataset_curator",
    "trainer",
    "operations_dashboard",
    "scope_guardian",
]


def seed_tenant_data(db: Session, tenant_id: str) -> None:
    """Idempotent-ish seed for a tenant's operational tables."""
    existing = (
        db.query(m.CategoryState)
        .filter(m.CategoryState.tenant_id == tenant_id)
        .count()
    )
    if existing:
        return

    for cat in CATEGORIES:
        target = 40 if cat in ("beauty", "tech", "fashion") else 25
        verified = {
            "beauty": 28,
            "tech": 18,
            "fashion": 12,
            "fitness": 20,
            "food": 15,
            "gaming": 8,
            "travel": 10,
            "finance": 6,
        }.get(cat, 5)
        db.add(
            m.CategoryState(
                id=_uid("cat_"),
                tenant_id=tenant_id,
                name=cat,
                phase_target=target,
                verified_count=verified,
                verification_rate_14d=0.55 + (hash(cat) % 30) / 100,
                cost_per_verified_14d=0.8 + (hash(cat) % 40) / 100,
                weeks_missed_target=2 if cat in ("gaming", "finance") else 0,
            )
        )

    for src, rel in [
        ("instagram", 0.78),
        ("tiktok", 0.72),
        ("youtube", 0.85),
        ("x", 0.65),
        ("blog", 0.70),
        ("phyllo", 0.90),
    ]:
        db.add(
            m.SourceReliability(
                id=_uid("sr_"), tenant_id=tenant_id, source=src, reliability=rel
            )
        )

    for name, kind, desc in [
        ("Brand", "entity", "A brand or company running a campaign"),
        ("Creator", "entity", "An influencer or content creator"),
        ("Campaign", "entity", "A specific sponsored campaign"),
        ("Platform", "entity", "Social platform"),
        ("Product", "entity", "Product being promoted"),
        ("sponsors", "relationship", "Creator sponsors/promotes Brand"),
        ("features", "relationship", "Campaign features Product"),
        ("runs_on", "relationship", "Campaign runs on Platform"),
        ("engagement_rate", "fact", "Numeric engagement rate"),
        ("campaign_budget_tier", "fact", "Budget tier label"),
        ("content_format", "fact", "Reel, short, long-form, etc."),
        ("call_to_action", "fact", "Primary CTA used"),
    ]:
        db.add(
            m.OntologyType(
                id=_uid("ont_"),
                tenant_id=tenant_id,
                type_name=name,
                kind=kind,
                description=desc,
            )
        )

    for i, (cat, src) in enumerate(
        [
            ("beauty", "instagram"),
            ("tech", "youtube"),
            ("fashion", "tiktok"),
            ("fitness", "instagram"),
            ("food", "tiktok"),
        ]
    ):
        db.add(
            m.WorkQueueItem(
                id=_uid("wq_"),
                tenant_id=tenant_id,
                category=cat,
                source=src,
                priority_score=0.9 - i * 0.1,
                assigned_agent="discovery",
                status="open",
            )
        )

    samples = [
        ("GlowSerum Co", "@maya_beauty", "beauty", "Summer Glow Challenge", "verified", 0.88),
        ("FitFuel", "@iron_lena", "fitness", "30-Day Protein Push", "verified", 0.82),
        ("NovaPhone", "@tech_with_jay", "tech", "Camera Night Mode Drop", "pending", 0.0),
        ("StreetThread", "@style_by_kim", "fashion", "Capsule Wardrobe Collab", "verified", 0.79),
        ("BiteBox", "@chef_rio", "food", "Mystery Meal Unbox", "pending", 0.0),
        ("PixelQuest", "@gamer_nova", "gaming", "Season Pass Giveaway", "rejected", 0.25),
    ]
    campaign_ids: list[str] = []
    for brand, creator, cat, title, status, conf in samples:
        cid = _uid("camp_")
        campaign_ids.append(cid)
        db.add(
            m.Campaign(
                id=cid,
                tenant_id=tenant_id,
                brand=brand,
                creator=creator,
                category=cat,
                title=title,
                verification_status=status,
                confidence=conf,
            )
        )
        db.add(
            m.CampaignEvidence(
                id=_uid("ce_"),
                tenant_id=tenant_id,
                campaign_id=cid,
                source="instagram" if cat != "tech" else "youtube",
                content_text=(
                    f"{creator} partnered with {brand} for '{title}'. "
                    f"Caption highlights product benefits and a limited-time code."
                ),
                confidence=0.75,
            )
        )

    for cid, (brand, creator, cat, title, status, conf) in zip(campaign_ids, samples):
        if status != "verified":
            continue
        for entity, ftype, value, rel in [
            (brand, "Brand", brand, None),
            (creator, "Creator", creator, None),
            (title, "Campaign", title, None),
            (creator, "sponsors", brand, "sponsors"),
            (title, "content_format", "short_video", None),
            (title, "engagement_rate", "4.2%", None),
        ]:
            db.add(
                m.GraphFact(
                    id=_uid("gf_"),
                    tenant_id=tenant_id,
                    entity=entity,
                    fact_type=ftype,
                    value=value,
                    relationship=rel,
                    citation=f"campaign_evidence:{cid}",
                    campaign_id=cid,
                )
            )

    fa, fb = _uid("gf_"), _uid("gf_")
    db.add(
        m.GraphFact(
            id=fa,
            tenant_id=tenant_id,
            entity="GlowSerum Co",
            fact_type="engagement_rate",
            value="4.2%",
            citation="src_a",
            campaign_id=campaign_ids[0],
        )
    )
    db.add(
        m.GraphFact(
            id=fb,
            tenant_id=tenant_id,
            entity="GlowSerum Co",
            fact_type="engagement_rate",
            value="7.8%",
            citation="src_b",
            campaign_id=campaign_ids[0],
        )
    )
    db.add(
        m.Contradiction(
            id=_uid("con_"),
            tenant_id=tenant_id,
            fact_a_id=fa,
            fact_b_id=fb,
            resolution_status="open",
        )
    )

    for cat, src, title, brand, creator in [
        ("beauty", "instagram", "New vitamin-C serum reel by @skin_sage", "Radiance Labs", "@skin_sage"),
        ("tech", "youtube", "NovaPhone unboxing long-form", "NovaPhone", "@tech_with_jay"),
        ("fashion", "tiktok", "Capsule wardrobe haul", "StreetThread", "@style_by_kim"),
        ("fitness", "instagram", "FitFuel PR challenge day 7", "FitFuel", "@iron_lena"),
        ("food", "tiktok", "BiteBox spicy mystery box", "BiteBox", "@chef_rio"),
    ]:
        db.add(
            m.DiscoveryCandidate(
                id=_uid("cand_"),
                tenant_id=tenant_id,
                category=cat,
                source=src,
                title=title,
                url=f"https://example.com/{src}/{brand.lower().replace(' ', '-')}",
                brand=brand,
                creator=creator,
                content_date=m.utcnow().date().isoformat(),
                status="pending",
            )
        )

    topic_defs = [
        {
            "topic": "campaign_strategy",
            "display_name": "Campaign strategy",
            "description": "Gold SFT examples for strategic campaign recommendations.",
            "inp": "Brand: mid-tier skincare. Goal: awareness. Creator tier: micro. Format: reels.",
            "out": "Prioritize 3-5 micro creators with authentic routine content; lead with UGC-style reels.",
            "pattern": "Match creator authenticity + format to funnel stage.",
            "sample": {
                "input": "Brand: mid-tier skincare. Goal: awareness. Creator tier: micro. Format: reels.",
                "output": "Prioritize 3-5 micro creators with authentic routine content.",
                "rationale": "Authenticity beats reach at awareness stage.",
                "difficulty": "canonical",
                "is_negative": False,
            },
        },
        {
            "topic": "budget_allocation",
            "display_name": "Budget allocation",
            "description": "Gold examples for allocating spend across categories.",
            "inp": "Total budget $50k. Categories: beauty 40%, fitness 30%, tech 30%.",
            "out": "Allocate beauty $20k, fitness $15k, tech $15k with contingency on tech.",
            "pattern": "Weight allocation by historical verified ROAS.",
            "sample": {
                "input": "Total budget $50k across beauty/fitness/tech.",
                "output": "Beauty $20k, fitness $15k, tech $15k; hold tech contingency.",
                "rationale": "Historical ROAS + exploration slice.",
                "difficulty": "moderate",
                "is_negative": False,
            },
        },
        {
            "topic": "creator_selection",
            "display_name": "Creator selection",
            "description": "Gold examples for shortlisting creators against a brief.",
            "inp": "Need creators for eco-fashion launch; audience 18-34.",
            "out": "Shortlist creators with eco content and engagement rate > 3%.",
            "pattern": "Filter on audience fit + authenticity, not follower count alone.",
            "sample": {
                "input": "Eco-fashion launch; audience 18-34.",
                "output": "Shortlist eco-consistent creators, ER > 3%, geo match.",
                "rationale": "Audience fit over vanity metrics.",
                "difficulty": "canonical",
                "is_negative": False,
            },
        },
    ]
    topic_keys = []
    for td in topic_defs:
        topic_keys.append(td["topic"])
        db.add(
            m.SeedMaterial(
                id=_uid("seed_"),
                tenant_id=tenant_id,
                topic=td["topic"],
                input_text=td["inp"],
                output_text=td["out"],
                reasoning_pattern=td["pattern"],
            )
        )
        db.add(
            m.TopicSchema(
                id=_uid("ts_"),
                tenant_id=tenant_id,
                topic=td["topic"],
                display_name=td["display_name"],
                description=td["description"],
                schema_json=json.dumps(
                    {
                        "type": "object",
                        "required": ["input", "output", "difficulty"],
                        "properties": {
                            "input": {"type": "string", "description": "Model input / user brief"},
                            "output": {"type": "string", "description": "Ideal model output"},
                            "rationale": {"type": "string", "description": "Why this output is correct"},
                            "difficulty": {
                                "type": "string",
                                "enum": ["canonical", "moderate", "edge-case"],
                            },
                            "is_negative": {
                                "type": "boolean",
                                "description": "True if this is a negative/counterfactual example",
                            },
                        },
                    }
                ),
                sample_row_json=json.dumps(td["sample"]),
                export_format="jsonl",
                is_active=True,
            )
        )

    # Default research brief for this tenant (demo domain; replace via UI for other trainings)
    db.add(
        m.ResearchProject(
            id=_uid("prj_"),
            tenant_id=tenant_id,
            slug="default",
            name="Default gold-mining project",
            domain=(
                "Influencer marketing strategy — mine verified campaign patterns and "
                "turn them into high-quality supervised training examples."
            ),
            mission=(
                "Build gold SFT datasets for campaign strategy, budget allocation, and "
                "creator selection. Prefer high-evidence, cited, review-hardened examples."
            ),
            research_questions_json=json.dumps(
                [
                    "Which creator tiers and formats drive save-rate for awareness?",
                    "How should mid-size budgets allocate across categories given ROAS history?",
                    "What filters produce high-fit creator shortlists without vanity metrics?",
                ]
            ),
            sources_json=json.dumps(["instagram", "tiktok", "youtube", "x", "blog"]),
            categories_json=json.dumps(
                ["beauty", "fitness", "tech", "food", "fashion", "gaming", "travel", "finance"]
            ),
            phase_targets_json=json.dumps(
                {
                    "beauty": 40,
                    "tech": 40,
                    "fashion": 40,
                    "fitness": 25,
                    "food": 25,
                    "gaming": 25,
                    "travel": 25,
                    "finance": 25,
                }
            ),
            success_metrics_json=json.dumps(
                [
                    {"name": "verified_campaigns", "target": "meet phase targets"},
                    {"name": "approved_training_examples", "target": "growing per topic"},
                    {"name": "benchmark_coverage", "target": "20/50/30 difficulty mix"},
                    {"name": "open_escalations", "target": "resolved within 14 days"},
                ]
            ),
            topic_keys_json=json.dumps(topic_keys),
            agent_instructions=(
                "Treat every pipeline stage as gold-data mining for LLM training. "
                "Do not invent facts. Prefer escalation over low-confidence commits. "
                "All synthetic examples must obey the active topic schema."
            ),
            output_notes=(
                "Export JSONL with fields: input, output, rationale, difficulty, is_negative, "
                "topic, split. Benchmark examples must never appear in train splits."
            ),
            is_active=True,
        )
    )

    db.add(
        m.Contract(
            id=_uid("ctr_"),
            tenant_id=tenant_id,
            client_name="Default Client",
            allowed_categories_json=json.dumps(["beauty", "fashion", "fitness"]),
            excluded_competitors_json=json.dumps(["RivalGlow", "FastFit Inc"]),
            allowed_sources_json=json.dumps(["instagram", "tiktok", "youtube"]),
            brand_voice="Warm, evidence-led, never aggressive discounting language.",
            version=1,
            active=True,
        )
    )

    db.add(
        m.ResearchMemory(
            id=_uid("rm_"),
            tenant_id=tenant_id,
            topic="beauty_micro_creators",
            summary="Micro beauty creators with routine-first content outperform product-demo macros on save rate.",
        )
    )

    db.add(
        m.ModelRecord(
            id=f"model_baseline_{tenant_id[:8]}",
            tenant_id=tenant_id,
            dataset_version="ds_v0",
            training_config_json=json.dumps({"epochs": 3, "lr": 2e-5, "base": "sim"}),
            eval_scores_json=json.dumps(
                {
                    "campaign_strategy": 0.72,
                    "budget_allocation": 0.68,
                    "creator_selection": 0.70,
                    "aggregate": 0.70,
                }
            ),
            git_commit="seed000",
            is_production=True,
        )
    )

    for agent in AGENT_KEYS:
        db.add(
            m.AgentHealth(
                id=_uid("ah_"),
                tenant_id=tenant_id,
                agent=agent,
                last_status="never",
            )
        )

    db.add(
        m.EventLog(
            id=_uid("evt_"),
            tenant_id=tenant_id,
            agent="system",
            event_type="seed",
            message="Seeded tenant demo data",
            payload_json="{}",
        )
    )


def ensure_research_project_for_tenant(db: Session, tenant_id: str) -> None:
    """Backfill a default research project if tenant has none (existing DBs)."""
    exists = (
        db.query(m.ResearchProject)
        .filter_by(tenant_id=tenant_id)
        .first()
    )
    if exists:
        return
    # Minimal brief; user should edit in UI
    cats = (
        db.query(m.CategoryState)
        .filter_by(tenant_id=tenant_id)
        .all()
    )
    topics = (
        db.query(m.TopicSchema)
        .filter_by(tenant_id=tenant_id)
        .all()
    )
    db.add(
        m.ResearchProject(
            id=_uid("prj_"),
            tenant_id=tenant_id,
            slug="default",
            name="Default gold-mining project",
            domain="Edit this research brief to define what gold data to mine.",
            mission="Mine high-quality training examples for the active topic schemas.",
            research_questions_json=json.dumps(
                ["What patterns produce reliable, high-value training examples?"]
            ),
            sources_json=json.dumps(["web", "docs", "internal"]),
            categories_json=json.dumps([c.name for c in cats] or ["general"]),
            phase_targets_json=json.dumps(
                {c.name: c.phase_target for c in cats} or {"general": 50}
            ),
            success_metrics_json=json.dumps(
                [
                    {"name": "approved_examples", "target": "grow weekly"},
                    {"name": "benchmark_coverage", "target": "balanced difficulty"},
                ]
            ),
            topic_keys_json=json.dumps([t.topic for t in topics]),
            agent_instructions="Follow the research brief. Prefer quality over volume.",
            output_notes="Export JSONL matching each topic schema.",
            is_active=True,
        )
    )


def bootstrap(db: Session) -> None:
    settings = get_settings()
    admin = db.query(m.User).filter(m.User.is_superadmin.is_(True)).first()
    if not admin:
        admin = m.User(
            id=_uid("usr_"),
            email=settings.bootstrap_admin_email.lower(),
            hashed_password=hash_password(settings.bootstrap_admin_password),
            full_name="Platform Admin",
            is_superadmin=True,
            email_verified=True,
            password_set=True,
            is_active=True,
        )
        db.add(admin)
        db.flush()
    else:
        # Keep bootstrap admin usable after auth upgrades
        if not admin.email_verified:
            admin.email_verified = True
        if not admin.password_set:
            admin.password_set = True

    demo = db.query(m.Tenant).filter(m.Tenant.slug == "demo").first()
    if not demo:
        demo = m.Tenant(
            id=_uid("ten_"),
            slug="demo",
            name="Demo Tenant",
            plan="starter",
            monthly_budget_usd=settings.default_tenant_monthly_budget_usd,
        )
        db.add(demo)
        db.flush()
        db.add(
            m.Membership(
                id=_uid("mem_"),
                user_id=admin.id,
                tenant_id=demo.id,
                role="owner",
            )
        )
        seed_tenant_data(db, demo.id)

    # Backfill research projects for any tenant missing one
    for tenant in db.query(m.Tenant).all():
        ensure_research_project_for_tenant(db, tenant.id)

    db.commit()
