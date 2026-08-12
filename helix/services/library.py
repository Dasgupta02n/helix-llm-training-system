"""User-owned gold + synthetic library (indefinite storage)."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from helix.config import get_settings
from helix.db import models as m

# Deterministic cutoff for bootstrap/demo seed rows (one-time migration marker).
# Rows created before this on the demo/bootstrap tenant with demo topics → seed.
SEED_CUTOFF_UTC = datetime(2026, 8, 10, 0, 0, 0, tzinfo=timezone.utc)
SEED_MIGRATION_VERSION = "seed_v1_2026_08_10"

# Parameters users can choose to vary when synthesizing from gold
AVAILABLE_VARY_PARAMETERS: list[dict[str, str]] = [
    {
        "key": "tone",
        "label": "Tone of voice",
        "description": "e.g. formal, friendly, concise, empathetic",
    },
    {
        "key": "difficulty",
        "label": "Difficulty",
        "description": "easier, standard, or edge-case versions",
    },
    {
        "key": "persona",
        "label": "User persona",
        "description": "different customer / user types",
    },
    {
        "key": "context",
        "label": "Situation / context",
        "description": "background details around the same core problem",
    },
    {
        "key": "locale",
        "label": "Locale / phrasing",
        "description": "regional English phrasing without changing meaning",
    },
    {
        "key": "length",
        "label": "Answer length",
        "description": "shorter or more detailed ideal answers",
    },
    {
        "key": "channel",
        "label": "Channel",
        "description": "email vs chat vs ticket-style wording",
    },
]


def _uid(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def get_or_create_scope(db: Session, user_id: str, tenant_id: str) -> m.UserDataScope:
    settings = get_settings()
    row = (
        db.query(m.UserDataScope)
        .filter_by(user_id=user_id, tenant_id=tenant_id)
        .first()
    )
    if row:
        return row
    row = m.UserDataScope(
        id=_uid("scp_"),
        user_id=user_id,
        tenant_id=tenant_id,
        gold_target_count=settings.default_gold_target_count,
        variations_per_gold=settings.default_variations_per_gold,
        vary_parameters_json=json.dumps(
            [p["key"] for p in AVAILABLE_VARY_PARAMETERS[:5]]
        ),
        auto_promote_approved=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def scope_to_dict(row: m.UserDataScope) -> dict[str, Any]:
    try:
        params = json.loads(row.vary_parameters_json or "[]")
    except json.JSONDecodeError:
        params = []
    synthesized_target = int(row.gold_target_count) * int(row.variations_per_gold)
    return {
        "id": row.id,
        "user_id": row.user_id,
        "tenant_id": row.tenant_id,
        "gold_target_count": row.gold_target_count,
        "variations_per_gold": row.variations_per_gold,
        "synthesized_target_count": synthesized_target,
        "vary_parameters": params,
        "auto_promote_approved": bool(row.auto_promote_approved),
        "available_parameters": AVAILABLE_VARY_PARAMETERS,
        "retention": "indefinite",
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def update_scope(
    db: Session,
    user_id: str,
    tenant_id: str,
    *,
    gold_target_count: int | None = None,
    variations_per_gold: int | None = None,
    vary_parameters: list[str] | None = None,
    auto_promote_approved: bool | None = None,
) -> m.UserDataScope:
    settings = get_settings()
    row = get_or_create_scope(db, user_id, tenant_id)
    if gold_target_count is not None:
        row.gold_target_count = max(1, min(int(gold_target_count), 1_000_000))
    if variations_per_gold is not None:
        row.variations_per_gold = max(
            1, min(int(variations_per_gold), settings.max_variations_per_gold)
        )
    if vary_parameters is not None:
        allowed = {p["key"] for p in AVAILABLE_VARY_PARAMETERS}
        cleaned = [p for p in vary_parameters if p in allowed]
        row.vary_parameters_json = json.dumps(cleaned or ["tone", "difficulty"])
    if auto_promote_approved is not None:
        row.auto_promote_approved = bool(auto_promote_approved)
    row.updated_at = _now()
    db.commit()
    db.refresh(row)
    return row


_DEMO_TOPICS = {
    "campaign_strategy",
    "budget_allocation",
    "creator_selection",
    "beauty",
    "fitness",
    "fashion",
    "gaming",
    "travel",
    "food",
    "tech",
}


def _meta_dict(raw: str | None) -> dict:
    try:
        d = json.loads(raw or "{}")
        return d if isinstance(d, dict) else {}
    except json.JSONDecodeError:
        return {}


def _is_seed_kind(
    kind: str | None,
    topic: str | None = None,
    *,
    input_text: str | None = None,
    metadata_json: str | None = None,
    source_ref: str | None = None,
    created_at: datetime | None = None,
    tenant_slug: str | None = None,
) -> bool:
    if (kind or "").lower() in {"seed", "demo", "bootstrap", "sample"}:
        return True
    meta = _meta_dict(metadata_json)
    if meta.get("is_seed") or meta.get("seed") or meta.get("demo"):
        return True
    if meta.get("seed_migration") == SEED_MIGRATION_VERSION:
        return True
    t = (topic or "").lower().strip()
    # Deterministic legacy rule: pre-cutoff demo-topic rows (bootstrap era)
    created = created_at
    if created and created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    if created and created < SEED_CUTOFF_UTC and t in _DEMO_TOPICS:
        return True
    # Old lean pipeline format used for bootstrap influencer demos
    inp = input_text or ""
    if inp.startswith("Campaign brief:") or "Verified campaign" in (inp[:80] + ("")):
        return True
    # Bootstrap campaigns were never prefixed cand:
    if source_ref and not str(source_ref).startswith("cand:") and t in _DEMO_TOPICS:
        if created is None or (created and created < SEED_CUTOFF_UTC):
            return True
    # demo tenant bootstrap only when pre-cutoff
    if tenant_slug in {"demo", "helix-demo"} and t in _DEMO_TOPICS:
        if created is None or (created and created < SEED_CUTOFF_UTC):
            return True
    return False


def backfill_seed_marks(db: Session, user_id: str, tenant_id: str) -> int:
    """
    Deterministic one-time-ish seed migration.

    Marks rows as seed when:
    - already tagged seed/demo, OR
    - created before SEED_CUTOFF_UTC with demo topics / campaign-brief format, OR
    - metadata already carries seed_migration version.

    Idempotent: re-running only updates rows not yet source_kind=seed.
    """
    tenant = db.query(m.Tenant).filter_by(id=tenant_id).first()
    slug = tenant.slug if tenant else None
    rows = (
        db.query(m.GoldExample)
        .filter_by(owner_user_id=user_id, tenant_id=tenant_id, is_archived=False)
        .all()
    )
    n = 0
    for g in rows:
        if _is_seed_kind(
            g.source_kind,
            g.topic,
            input_text=g.input_text,
            metadata_json=g.metadata_json,
            source_ref=g.source_ref,
            created_at=g.created_at,
            tenant_slug=slug,
        ):
            meta = _meta_dict(g.metadata_json)
            needs = (g.source_kind or "").lower() != "seed" or not meta.get("is_seed")
            if needs:
                g.source_kind = "seed"
                meta["is_seed"] = True
                meta["seed_migration"] = SEED_MIGRATION_VERSION
                meta["seed_cutoff"] = SEED_CUTOFF_UTC.isoformat()
                g.metadata_json = json.dumps(meta)
                n += 1
    if n:
        db.commit()
    return n


def _token_set(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{3,}", (text or "").lower()) if w}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def find_near_duplicate_gold(
    db: Session,
    *,
    owner_user_id: str,
    tenant_id: str,
    input_text: str,
    output_text: str,
    threshold: float = 0.82,
    limit_scan: int = 400,
) -> m.GoldExample | None:
    """
    Semantic-ish near-duplicate detection against the existing gold set.
    Uses token Jaccard on input+output (fast, no embedding service required).
    """
    target = _token_set(f"{input_text}\n{output_text}")
    if len(target) < 8:
        return None
    rows = (
        db.query(m.GoldExample)
        .filter_by(
            owner_user_id=owner_user_id,
            tenant_id=tenant_id,
            is_archived=False,
        )
        .order_by(m.GoldExample.created_at.desc())
        .limit(limit_scan)
        .all()
    )
    best: m.GoldExample | None = None
    best_score = 0.0
    for g in rows:
        score = _jaccard(target, _token_set(f"{g.input_text}\n{g.output_text}"))
        if score >= threshold and score > best_score:
            best = g
            best_score = score
    return best


def library_stats(db: Session, user_id: str, tenant_id: str) -> dict[str, Any]:
    scope = get_or_create_scope(db, user_id, tenant_id)
    gold_rows = (
        db.query(m.GoldExample)
        .filter_by(owner_user_id=user_id, tenant_id=tenant_id, is_archived=False)
        .all()
    )
    gold_count = len(gold_rows)
    tenant = db.query(m.Tenant).filter_by(id=tenant_id).first()
    slug = tenant.slug if tenant else None
    seed_gold = sum(
        1
        for g in gold_rows
        if _is_seed_kind(
            g.source_kind,
            g.topic,
            input_text=g.input_text,
            metadata_json=g.metadata_json,
            source_ref=g.source_ref,
            created_at=g.created_at,
            tenant_slug=slug,
        )
    )
    user_gold = gold_count - seed_gold
    synth_count = (
        db.query(m.SyntheticExample)
        .filter_by(owner_user_id=user_id, tenant_id=tenant_id, is_archived=False)
        .count()
    )
    target_synth = scope.gold_target_count * scope.variations_per_gold
    return {
        "gold_count": gold_count,
        "gold_seed_count": seed_gold,
        "gold_user_count": user_gold,
        "gold_target": scope.gold_target_count,
        "gold_remaining": max(0, scope.gold_target_count - gold_count),
        "gold_progress_pct": min(
            100.0, round(100.0 * gold_count / max(scope.gold_target_count, 1), 1)
        ),
        "synthetic_count": synth_count,
        "synthetic_target": target_synth,
        "synthetic_remaining": max(0, target_synth - synth_count),
        "synthetic_progress_pct": min(
            100.0, round(100.0 * synth_count / max(target_synth, 1), 1)
        ),
        "variations_per_gold": scope.variations_per_gold,
        "retention": "indefinite",
        "scope": scope_to_dict(scope),
    }


def gold_to_dict(g: m.GoldExample, tenant_slug: str | None = None) -> dict[str, Any]:
    seed = _is_seed_kind(
        g.source_kind,
        g.topic,
        input_text=g.input_text,
        metadata_json=g.metadata_json,
        source_ref=g.source_ref,
        created_at=g.created_at,
        tenant_slug=tenant_slug,
    )
    if seed:
        origin = "Seed / demo"
    elif (g.source_kind or "").lower() == "corpus":
        origin = "Your corpus"
    elif (g.source_kind or "").lower() in {"pipeline", "mined"}:
        origin = "Mined (pipeline)"
    else:
        origin = "Your generated data"
    return {
        "id": g.id,
        "owner_user_id": g.owner_user_id,
        "tenant_id": g.tenant_id,
        "topic": g.topic,
        "input": g.input_text,
        "output": g.output_text,
        "rationale": g.rationale,
        "difficulty": g.difficulty,
        "is_negative": g.is_negative,
        "source_kind": "seed" if seed else (g.source_kind or "pipeline"),
        "source_ref": g.source_ref,
        "is_seed": seed,
        "origin_label": origin,
        "verification_status": g.verification_status,
        "created_at": g.created_at.isoformat() if g.created_at else None,
        "kind": "gold",
    }


def synthetic_to_dict(s: m.SyntheticExample) -> dict[str, Any]:
    try:
        varied = json.loads(s.varied_parameters_json or "{}")
    except json.JSONDecodeError:
        varied = {}
    return {
        "id": s.id,
        "owner_user_id": s.owner_user_id,
        "tenant_id": s.tenant_id,
        "gold_id": s.gold_id,
        "topic": s.topic,
        "input": s.input_text,
        "output": s.output_text,
        "rationale": s.rationale,
        "difficulty": s.difficulty,
        "is_negative": s.is_negative,
        "variation_index": s.variation_index,
        "varied_parameters": varied,
        "synthesis_run_id": s.synthesis_run_id,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "kind": "synthetic",
    }


def add_gold_example(
    db: Session,
    *,
    owner_user_id: str,
    tenant_id: str,
    topic: str,
    input_text: str,
    output_text: str,
    rationale: str | None = None,
    difficulty: str = "moderate",
    is_negative: bool = False,
    source_kind: str = "curated",
    source_ref: str | None = None,
    verification_status: str = "verified",
    metadata: dict | None = None,
    enforce_cap: bool = True,
) -> m.GoldExample | None:
    """Add gold to user account. Returns None if gold target already reached."""
    scope = get_or_create_scope(db, owner_user_id, tenant_id)
    if enforce_cap:
        count = (
            db.query(m.GoldExample)
            .filter_by(
                owner_user_id=owner_user_id,
                tenant_id=tenant_id,
                is_archived=False,
            )
            .count()
        )
        if count >= scope.gold_target_count:
            return None

    # Dedup by exact input+output for same user
    existing = (
        db.query(m.GoldExample)
        .filter_by(
            owner_user_id=owner_user_id,
            tenant_id=tenant_id,
            input_text=input_text,
            output_text=output_text,
            is_archived=False,
        )
        .first()
    )
    if existing:
        return existing

    # Near-duplicate against full gold set (not just exact match / per-batch)
    near = find_near_duplicate_gold(
        db,
        owner_user_id=owner_user_id,
        tenant_id=tenant_id,
        input_text=input_text,
        output_text=output_text,
    )
    if near:
        return near

    g = m.GoldExample(
        id=_uid("gold_"),
        owner_user_id=owner_user_id,
        tenant_id=tenant_id,
        topic=topic or "general",
        input_text=input_text,
        output_text=output_text,
        rationale=rationale,
        difficulty=difficulty or "moderate",
        is_negative=is_negative,
        source_kind=source_kind,
        source_ref=source_ref,
        verification_status=verification_status,
        metadata_json=json.dumps(metadata or {}),
    )
    db.add(g)
    db.commit()
    db.refresh(g)
    return g


def promote_training_example_to_gold(
    db: Session,
    example: m.TrainingExample,
    owner_user_id: str,
) -> m.GoldExample | None:
    return add_gold_example(
        db,
        owner_user_id=owner_user_id,
        tenant_id=example.tenant_id,
        topic=example.topic,
        input_text=example.input_text,
        output_text=example.output_text,
        rationale=example.rationale,
        difficulty=example.difficulty,
        is_negative=example.is_negative,
        source_kind="pipeline",
        source_ref=example.id,
        verification_status="verified",
        metadata={"review_status": example.review_status},
    )


def promote_approved_pool(
    db: Session,
    *,
    owner_user_id: str,
    tenant_id: str,
    limit: int | None = None,
) -> dict[str, Any]:
    """Promote approved non-benchmark training examples into user gold library."""
    scope = get_or_create_scope(db, owner_user_id, tenant_id)
    before = (
        db.query(m.GoldExample)
        .filter_by(owner_user_id=owner_user_id, tenant_id=tenant_id, is_archived=False)
        .count()
    )
    remaining = max(0, scope.gold_target_count - before)
    if remaining <= 0:
        return {
            "promoted": 0,
            "skipped": 0,
            "message": "Gold target already reached",
            "stats": library_stats(db, owner_user_id, tenant_id),
        }

    cap = remaining if limit is None else min(remaining, limit)
    rows = (
        db.query(m.TrainingExample)
        .filter_by(
            tenant_id=tenant_id,
            review_status="approved",
            reserved_for_benchmark=False,
        )
        .order_by(m.TrainingExample.created_at.desc())
        .limit(cap * 3)
        .all()
    )
    promoted = 0
    skipped = 0
    for r in rows:
        if promoted >= cap:
            break
        # already promoted?
        if (
            db.query(m.GoldExample)
            .filter_by(owner_user_id=owner_user_id, source_ref=r.id, is_archived=False)
            .first()
        ):
            skipped += 1
            continue
        g = promote_training_example_to_gold(db, r, owner_user_id)
        if g is None:
            skipped += 1
            break  # cap reached
        if g.source_ref == r.id:
            promoted += 1
        else:
            skipped += 1

    return {
        "promoted": promoted,
        "skipped": skipped,
        "stats": library_stats(db, owner_user_id, tenant_id),
    }
