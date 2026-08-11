"""User-owned gold + synthetic library (indefinite storage)."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from helix.config import get_settings
from helix.db import models as m

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
) -> bool:
    if (kind or "").lower() in {"seed", "demo", "bootstrap", "sample"}:
        return True
    meta = _meta_dict(metadata_json)
    if meta.get("is_seed") or meta.get("seed") or meta.get("demo"):
        return True
    t = (topic or "").lower().strip()
    if t in _DEMO_TOPICS:
        return True
    # Old lean pipeline format used for bootstrap influencer demos
    inp = input_text or ""
    if inp.startswith("Campaign brief:") or "Verified campaign" in (inp[:80] + ("")):
        return True
    # Bootstrap campaigns were never prefixed cand:
    if source_ref and not str(source_ref).startswith("cand:") and t in _DEMO_TOPICS:
        return True
    return False


def backfill_seed_marks(db: Session, user_id: str, tenant_id: str) -> int:
    """Ensure legacy demo/bootstrap gold rows are consistently tagged source_kind=seed."""
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
        ):
            if (g.source_kind or "").lower() != "seed":
                g.source_kind = "seed"
                meta = _meta_dict(g.metadata_json)
                meta["is_seed"] = True
                meta["seed_backfill"] = True
                g.metadata_json = json.dumps(meta)
                n += 1
    if n:
        db.commit()
    return n


def library_stats(db: Session, user_id: str, tenant_id: str) -> dict[str, Any]:
    scope = get_or_create_scope(db, user_id, tenant_id)
    gold_rows = (
        db.query(m.GoldExample)
        .filter_by(owner_user_id=user_id, tenant_id=tenant_id, is_archived=False)
        .all()
    )
    gold_count = len(gold_rows)
    seed_gold = sum(
        1
        for g in gold_rows
        if _is_seed_kind(
            g.source_kind,
            g.topic,
            input_text=g.input_text,
            metadata_json=g.metadata_json,
            source_ref=g.source_ref,
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


def gold_to_dict(g: m.GoldExample) -> dict[str, Any]:
    seed = _is_seed_kind(
        g.source_kind,
        g.topic,
        input_text=g.input_text,
        metadata_json=g.metadata_json,
        source_ref=g.source_ref,
    )
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
        "origin_label": "Seed / demo" if seed else "Your generated data",
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
