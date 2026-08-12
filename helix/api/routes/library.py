"""User account library: gold targets, synthesis, indefinite storage."""

from __future__ import annotations

import json
import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from helix.api.deps import get_current_user
from helix.db import models as m
from helix.db.session import get_db
from helix.services.corpus import (
    add_paste,
    add_url,
    archive_document,
    document_to_dict,
    list_corpus,
)
from helix.services.gold_quality import (
    backfill_quality_on_gold_rows,
    backfill_reject_cross_domain_gold,
)
from helix.services.library import (
    add_gold_example,
    backfill_seed_marks,
    get_or_create_scope,
    gold_to_dict,
    library_stats,
    promote_approved_pool,
    promote_training_example_to_gold,
    scope_to_dict,
    synthetic_to_dict,
    update_scope,
)
from helix.services.synthesis import run_synthesis

router = APIRouter(prefix="/api/t/{slug}/library", tags=["library"])


def _tenant_for(user: m.User, slug: str, db: Session) -> m.Tenant:
    tenant = db.query(m.Tenant).filter_by(slug=slug, is_active=True).first()
    if not tenant:
        raise HTTPException(404, "Workspace not found")
    if not user.is_superadmin:
        mem = (
            db.query(m.Membership)
            .filter_by(user_id=user.id, tenant_id=tenant.id)
            .first()
        )
        if not mem:
            raise HTTPException(403, "You do not have access to this workspace")
    return tenant


class ScopeUpdate(BaseModel):
    gold_target_count: int | None = Field(default=None, ge=1, le=1_000_000)
    variations_per_gold: int | None = Field(default=None, ge=1, le=20)
    vary_parameters: list[str] | None = None
    auto_promote_approved: bool | None = None


class GoldCreate(BaseModel):
    topic: str = "general"
    input: str
    output: str
    rationale: str | None = None
    difficulty: str = "moderate"
    is_negative: bool = False


class PromoteRequest(BaseModel):
    limit: int | None = Field(default=None, ge=1, le=50_000)
    example_ids: list[str] | None = None


class SynthesizeRequest(BaseModel):
    variations_per_gold: int | None = Field(default=None, ge=1, le=20)
    parameters: list[str] | None = None
    gold_ids: list[str] | None = None
    max_golds: int | None = Field(default=None, ge=1, le=200)
    use_llm: bool = True


class CorpusPasteRequest(BaseModel):
    title: str = "Pasted document"
    content: str = Field(..., min_length=20)
    category: str = "general"


class CorpusUrlRequest(BaseModel):
    url: str = Field(..., min_length=8)
    title: str = ""
    category: str = "general"
    fetch: bool = True


@router.get("/settings")
def get_settings(
    slug: str,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    tenant = _tenant_for(user, slug, db)
    scope = get_or_create_scope(db, user.id, tenant.id)
    return scope_to_dict(scope)


@router.put("/settings")
def put_settings(
    slug: str,
    body: ScopeUpdate,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    tenant = _tenant_for(user, slug, db)
    scope = update_scope(
        db,
        user.id,
        tenant.id,
        gold_target_count=body.gold_target_count,
        variations_per_gold=body.variations_per_gold,
        vary_parameters=body.vary_parameters,
        auto_promote_approved=body.auto_promote_approved,
    )
    return scope_to_dict(scope)


@router.get("/stats")
def stats(
    slug: str,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    tenant = _tenant_for(user, slug, db)
    backfill_seed_marks(db, user.id, tenant.id)
    return library_stats(db, user.id, tenant.id)


@router.get("/gold")
def list_gold(
    slug: str,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    topic: str | None = None,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    tenant = _tenant_for(user, slug, db)
    backfill_seed_marks(db, user.id, tenant.id)
    q = db.query(m.GoldExample).filter_by(
        owner_user_id=user.id, tenant_id=tenant.id, is_archived=False
    )
    if topic:
        q = q.filter_by(topic=topic)
    total = q.count()
    rows = (
        q.order_by(m.GoldExample.created_at.desc()).offset(offset).limit(limit).all()
    )
    return {
        "total": total,
        "items": [gold_to_dict(r, tenant_slug=tenant.slug) for r in rows],
        "retention": "indefinite",
    }


@router.get("/corpus")
def get_corpus(
    slug: str,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    tenant = _tenant_for(user, slug, db)
    # Default: only corpus for the active research plan (prevents cross-plan UI mix)
    rows = list_corpus(
        db, tenant_id=tenant.id, owner_user_id=user.id, scope_to_plan=True
    )
    return {
        "items": [document_to_dict(r) for r in rows],
        "total": len(rows),
        "scoped_to_active_plan": True,
    }


@router.post("/corpus/paste")
def corpus_paste(
    slug: str,
    body: CorpusPasteRequest,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    tenant = _tenant_for(user, slug, db)
    out = add_paste(
        db,
        tenant_id=tenant.id,
        owner_user_id=user.id,
        title=body.title,
        content=body.content,
        category=body.category,
        # Bound to active research plan so mining for another plan cannot reuse it
        project_id=None,  # resolved inside add_paste via active_project_id
    )
    if not out.get("ok"):
        raise HTTPException(400, out.get("error") or "Failed to add document")
    return out


@router.post("/corpus/url")
def corpus_url(
    slug: str,
    body: CorpusUrlRequest,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    tenant = _tenant_for(user, slug, db)
    out = add_url(
        db,
        tenant_id=tenant.id,
        owner_user_id=user.id,
        url=body.url,
        title=body.title,
        category=body.category,
        fetch=body.fetch,
        project_id=None,
    )
    if not out.get("ok"):
        raise HTTPException(400, out.get("error") or "Failed to fetch URL")
    return out


@router.delete("/corpus/{doc_id}")
def corpus_delete(
    slug: str,
    doc_id: str,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    tenant = _tenant_for(user, slug, db)
    out = archive_document(
        db, tenant_id=tenant.id, doc_id=doc_id, owner_user_id=user.id
    )
    if not out.get("ok"):
        raise HTTPException(400, out.get("error") or "Failed")
    return out


@router.post("/quality-backfill")
def quality_backfill(
    slug: str,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Re-run quality gates on all historical gold for this user/workspace."""
    tenant = _tenant_for(user, slug, db)
    result = backfill_quality_on_gold_rows(
        db, owner_user_id=user.id, tenant_id=tenant.id
    )
    cross = backfill_reject_cross_domain_gold(
        db, owner_user_id=user.id, tenant_id=tenant.id
    )
    return {"ok": True, **result, "cross_domain": cross}


@router.post("/cross-domain-backfill")
def cross_domain_backfill(
    slug: str,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """
    One-shot: reject gold poisoned by another plan's corpus
    (e.g. food-delivery FAQ labeled as HR/PTO).
    """
    tenant = _tenant_for(user, slug, db)
    result = backfill_reject_cross_domain_gold(
        db, owner_user_id=user.id, tenant_id=tenant.id
    )
    return {"ok": True, **result}


@router.post("/gold")
def create_gold(
    slug: str,
    body: GoldCreate,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    tenant = _tenant_for(user, slug, db)
    g = add_gold_example(
        db,
        owner_user_id=user.id,
        tenant_id=tenant.id,
        topic=body.topic,
        input_text=body.input,
        output_text=body.output,
        rationale=body.rationale,
        difficulty=body.difficulty,
        is_negative=body.is_negative,
        source_kind="curated",
        verification_status="verified",
    )
    if g is None:
        raise HTTPException(
            400,
            "Gold target reached for your account. Raise the gold data goal in Settings, or archive older rows.",
        )
    return gold_to_dict(g)


@router.post("/gold/promote")
def promote_gold(
    slug: str,
    body: PromoteRequest,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Promote vetted pipeline examples into the user's permanent gold library."""
    tenant = _tenant_for(user, slug, db)
    if body.example_ids:
        promoted = 0
        for eid in body.example_ids:
            ex = (
                db.query(m.TrainingExample)
                .filter_by(id=eid, tenant_id=tenant.id)
                .first()
            )
            if not ex or ex.review_status != "approved":
                continue
            g = promote_training_example_to_gold(db, ex, user.id)
            if g:
                promoted += 1
        return {
            "promoted": promoted,
            "stats": library_stats(db, user.id, tenant.id),
            "message": f"Saved {promoted} gold examples to your account forever.",
        }
    result = promote_approved_pool(
        db, owner_user_id=user.id, tenant_id=tenant.id, limit=body.limit
    )
    result["message"] = (
        f"Saved gold examples to your account forever. "
        f"Promoted this run: {result.get('promoted', 0)}."
    )
    return result


@router.get("/synthetic")
def list_synthetic(
    slug: str,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    gold_id: str | None = None,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    tenant = _tenant_for(user, slug, db)
    q = db.query(m.SyntheticExample).filter_by(
        owner_user_id=user.id, tenant_id=tenant.id, is_archived=False
    )
    if gold_id:
        q = q.filter_by(gold_id=gold_id)
    total = q.count()
    rows = (
        q.order_by(m.SyntheticExample.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return {
        "total": total,
        "items": [synthetic_to_dict(r) for r in rows],
        "retention": "indefinite",
    }


@router.post("/synthesize")
def synthesize(
    slug: str,
    body: SynthesizeRequest,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    tenant = _tenant_for(user, slug, db)
    return run_synthesis(
        db,
        owner_user_id=user.id,
        tenant_id=tenant.id,
        variations_per_gold=body.variations_per_gold,
        parameters=body.parameters,
        gold_ids=body.gold_ids,
        max_golds=body.max_golds,
        use_llm=body.use_llm,
    )


@router.get("/runs")
def list_runs(
    slug: str,
    limit: int = Query(20, ge=1, le=100),
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    tenant = _tenant_for(user, slug, db)
    rows = (
        db.query(m.SynthesisRun)
        .filter_by(owner_user_id=user.id, tenant_id=tenant.id)
        .order_by(m.SynthesisRun.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": r.id,
            "status": r.status,
            "gold_processed": r.gold_processed,
            "synthesized_count": r.synthesized_count,
            "variations_per_gold": r.variations_per_gold,
            "parameters": json.loads(r.parameters_json or "[]"),
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            "error": r.error,
        }
        for r in rows
    ]


@router.get("/export")
def export_library(
    slug: str,
    kind: str = Query("all", pattern="^(all|gold|synthetic)$"),
    format: str = Query("jsonl", pattern="^(jsonl|json)$"),
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Export the signed-in user's permanent library (never expires)."""
    tenant = _tenant_for(user, slug, db)
    items: list[dict[str, Any]] = []
    if kind in {"all", "gold"}:
        for g in (
            db.query(m.GoldExample)
            .filter_by(owner_user_id=user.id, tenant_id=tenant.id, is_archived=False)
            .all()
        ):
            items.append(gold_to_dict(g))
    if kind in {"all", "synthetic"}:
        for s in (
            db.query(m.SyntheticExample)
            .filter_by(owner_user_id=user.id, tenant_id=tenant.id, is_archived=False)
            .all()
        ):
            items.append(synthetic_to_dict(s))

    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", f"{slug}_{user.id[:8]}_{kind}")
    if format == "json":
        payload = json.dumps(items, ensure_ascii=False, indent=2)
        return PlainTextResponse(
            payload,
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="helix_library_{safe}.json"'
            },
        )

    def gen():
        for row in items:
            yield json.dumps(row, ensure_ascii=False) + "\n"

    return StreamingResponse(
        gen(),
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": f'attachment; filename="helix_library_{safe}.jsonl"'
        },
    )
