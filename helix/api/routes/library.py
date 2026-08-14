"""User account library: gold targets, synthesis, indefinite storage."""

from __future__ import annotations

import json
import re
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
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
from helix.services.user_gold_upload import (
    MAX_ZIP_BYTES,
    USER_UPLOAD_SOURCE_KIND,
    import_zip_as_gold,
)
from helix.services.user_material_upload import (
    USER_MATERIAL_SOURCE_KIND,
    import_material_zip_as_trainable,
)

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
    source: str | None = Query(
        None,
        description="Optional filter: user_upload | user_material | seed | pipeline | corpus",
    ),
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
    if source:
        src = source.strip().lower()
        if src in {"user_upload", "byo", "upload"}:
            q = q.filter(
                m.GoldExample.source_kind.in_(
                    [USER_UPLOAD_SOURCE_KIND, "byo", "upload"]
                )
            )
        elif src in {"user_material", "material", "materials"}:
            q = q.filter(
                m.GoldExample.source_kind.in_(
                    [USER_MATERIAL_SOURCE_KIND, "material", "materials"]
                )
            )
        else:
            q = q.filter_by(source_kind=src)
    total = q.count()
    rows = (
        q.order_by(m.GoldExample.created_at.desc()).offset(offset).limit(limit).all()
    )
    return {
        "total": total,
        "items": [gold_to_dict(r, tenant_slug=tenant.slug) for r in rows],
        "retention": "indefinite",
        "source_filter": source,
    }


@router.post("/gold/upload-zip")
async def upload_gold_zip(
    slug: str,
    file: UploadFile = File(...),
    topic: str = Form("user_upload"),
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """
    Upload a zip of labeled examples → saved as gold-format rows
    (source_kind=user_upload) for download + future Double Helix.
    """
    tenant = _tenant_for(user, slug, db)
    name = (file.filename or "upload.zip").lower()
    if not name.endswith(".zip"):
        raise HTTPException(400, "Please upload a .zip file")
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "Empty file")
    if len(raw) > MAX_ZIP_BYTES:
        raise HTTPException(
            400, f"Zip too large (max {MAX_ZIP_BYTES // (1024 * 1024)} MB)"
        )
    import io

    out = import_zip_as_gold(
        db,
        owner_user_id=user.id,
        tenant_id=tenant.id,
        fileobj=io.BytesIO(raw),
        filename=file.filename or "upload.zip",
        default_topic=(topic or "user_upload").strip()[:80] or "user_upload",
        enforce_cap=True,
    )
    if not out.get("ok"):
        raise HTTPException(400, out.get("error") or "Import failed")
    out["stats"] = library_stats(db, user.id, tenant.id)
    return out


@router.post("/gold/upload-materials-zip")
async def upload_materials_zip(
    slug: str,
    file: UploadFile = File(...),
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """
    Unlabeled materials zip (scripts, rulebooks, notes) → converted trainable
    gold-format rows (source_kind=user_material).
    """
    tenant = _tenant_for(user, slug, db)
    name = (file.filename or "materials.zip").lower()
    if not name.endswith(".zip"):
        raise HTTPException(400, "Please upload a .zip file")
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "Empty file")
    if len(raw) > MAX_ZIP_BYTES:
        raise HTTPException(
            400, f"Zip too large (max {MAX_ZIP_BYTES // (1024 * 1024)} MB)"
        )
    import io

    domain = ""
    try:
        from helix.services.brief import get_active_project, project_to_dict

        proj = get_active_project(db, tenant.id)
        if proj:
            domain = str(project_to_dict(proj).get("domain") or "")
    except Exception:  # noqa: BLE001
        pass

    out = import_material_zip_as_trainable(
        db,
        owner_user_id=user.id,
        tenant_id=tenant.id,
        fileobj=io.BytesIO(raw),
        filename=file.filename or "materials.zip",
        domain=domain,
        enforce_cap=True,
    )
    if not out.get("ok"):
        raise HTTPException(400, out.get("error") or "Import failed")
    out["stats"] = library_stats(db, user.id, tenant.id)
    return out


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
    # Superadmin cleans whole workspace; members clean only their own library.
    owner_id = None if user.is_superadmin else user.id
    result = backfill_quality_on_gold_rows(
        db, owner_user_id=owner_id or user.id, tenant_id=tenant.id
    )
    cross = backfill_reject_cross_domain_gold(
        db, owner_user_id=owner_id, tenant_id=tenant.id
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

    Members: own gold only. Superadmin: all gold in the workspace
    (so contaminated rows owned by other users still get cleaned).
    """
    tenant = _tenant_for(user, slug, db)
    owner_id = None if user.is_superadmin else user.id
    result = backfill_reject_cross_domain_gold(
        db, owner_user_id=owner_id, tenant_id=tenant.id
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
    kind: str = Query(
        "all",
        pattern="^(all|gold|synthetic|user_upload|user_material|trainable)$",
    ),
    format: str = Query("jsonl", pattern="^(jsonl|json|chat)$"),
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Export the signed-in user's permanent library (never expires).

    kind=user_upload → labeled BYO gold rows
    kind=user_material → converted unlabeled materials
    kind=trainable → all gold-format training rows (mined + uploads + materials)
    """
    tenant = _tenant_for(user, slug, db)
    items: list[dict[str, Any]] = []
    if kind in {
        "all",
        "gold",
        "user_upload",
        "user_material",
        "trainable",
    }:
        q = db.query(m.GoldExample).filter_by(
            owner_user_id=user.id, tenant_id=tenant.id, is_archived=False
        )
        if kind == "user_upload":
            q = q.filter(
                m.GoldExample.source_kind.in_(
                    [USER_UPLOAD_SOURCE_KIND, "byo", "upload"]
                )
            )
        elif kind == "user_material":
            q = q.filter(
                m.GoldExample.source_kind.in_(
                    [USER_MATERIAL_SOURCE_KIND, "material", "materials"]
                )
            )
        elif kind == "trainable":
            # All non-rejected gold (including uploads & materials)
            q = q.filter(
                m.GoldExample.verification_status != "rejected"
            )
        for g in q.order_by(m.GoldExample.created_at.asc()).all():
            items.append(gold_to_dict(g, tenant_slug=tenant.slug))
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

    def _chat_row(row: dict[str, Any]) -> dict[str, Any]:
        from helix.services.library import gold_to_chat_messages

        return gold_to_chat_messages(
            row.get("input") or row.get("input_text") or "",
            row.get("output") or row.get("output_text") or "",
        )

    def gen():
        slim_kinds = {
            USER_UPLOAD_SOURCE_KIND,
            "byo",
            "upload",
            USER_MATERIAL_SOURCE_KIND,
            "material",
            "materials",
        }
        for row in items:
            if format == "chat":
                yield json.dumps(_chat_row(row), ensure_ascii=False) + "\n"
                continue
            # Stable training shape for Double Helix / external trainers
            if kind in {
                "user_upload",
                "user_material",
                "trainable",
            } or row.get("source_kind") in slim_kinds:
                slim = {
                    "id": row.get("id"),
                    "topic": row.get("topic"),
                    "input": row.get("input"),
                    "output": row.get("output"),
                    "rationale": row.get("rationale"),
                    "difficulty": row.get("difficulty"),
                    "is_negative": row.get("is_negative"),
                    "source_kind": row.get("source_kind"),
                    "kind": "gold",
                }
                yield json.dumps(slim, ensure_ascii=False) + "\n"
            else:
                yield json.dumps(row, ensure_ascii=False) + "\n"

    fname = f"helix_library_{safe}"
    if format == "chat":
        fname += "_chat"
    return StreamingResponse(
        gen(),
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": f'attachment; filename="{fname}.jsonl"'
        },
    )


@router.get("/double-helix/models")
def double_helix_models(
    slug: str,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    _tenant_for(user, slug, db)
    from helix.services.base_models import public_models

    from helix.services.runpod_train import compute_policy

    return {
        "max_params_b": 30,
        "licenses": ["Apache-2.0", "MIT"],
        "excluded": ["Llama (Meta Community License)", "Gemma (Gemma license)"],
        "models": public_models(),
        "compute": compute_policy(),
    }


@router.post("/double-helix/package")
def double_helix_package(
    slug: str,
    model_id: str | None = Query(None),
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Zip chat-format gold + Apache/MIT model notes. Admin-approved accounts only."""
    if not (user.admin_approved or user.is_superadmin):
        raise HTTPException(403, "Double Helix packaging is limited to approved accounts.")
    tenant = _tenant_for(user, slug, db)
    from helix.services.brief import get_active_project

    proj = get_active_project(db, tenant.id)
    if not model_id and proj and proj.agent_instructions and "MODEL:" in proj.agent_instructions:
        model_id = proj.agent_instructions.split("MODEL:", 1)[1].split("\n", 1)[0].strip()
    rows = (
        db.query(m.GoldExample)
        .filter_by(
            owner_user_id=user.id,
            tenant_id=tenant.id,
            is_archived=False,
        )
        .filter(m.GoldExample.verification_status != "rejected")
        .order_by(m.GoldExample.created_at.asc())
        .all()
    )
    payload = [
        {"input": g.input_text, "output": g.output_text, "id": g.id} for g in rows
    ]
    from helix.services.double_helix import build_package_zip

    blob = build_package_zip(payload, model_id=model_id)
    return StreamingResponse(
        iter([blob]),
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="helix_double_helix_v1.zip"'
        },
    )


class DoubleHelixTrainRequest(BaseModel):
    model_id: str | None = None
    confirm: bool = False


def _require_approved(user: m.User) -> None:
    if not (user.admin_approved or user.is_superadmin):
        raise HTTPException(403, "Double Helix is limited to approved accounts.")


@router.post("/double-helix/train")
def double_helix_train_start(
    slug: str,
    body: DoubleHelixTrainRequest,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Start QLoRA on gold already in this account. Does not replace data download."""
    _require_approved(user)
    tenant = _tenant_for(user, slug, db)
    from helix.services.brief import get_active_project
    from helix.services.double_helix_train import create_train_job, job_to_dict

    model_id = body.model_id
    if not model_id:
        proj = get_active_project(db, tenant.id)
        if proj and proj.agent_instructions and "MODEL:" in proj.agent_instructions:
            model_id = proj.agent_instructions.split("MODEL:", 1)[1].split("\n", 1)[0].strip()
    try:
        job = create_train_job(
            db,
            owner_user_id=user.id,
            tenant_id=tenant.id,
            model_id=model_id,
            confirm=bool(body.confirm),
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True, "job": job_to_dict(job)}


@router.get("/double-helix/train")
def double_helix_train_list(
    slug: str,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    _require_approved(user)
    tenant = _tenant_for(user, slug, db)
    from helix.services.double_helix_train import (
        job_to_dict,
        latest_train_job,
        tick_train_job,
    )

    job = latest_train_job(db, owner_user_id=user.id, tenant_id=tenant.id)
    # Worker starts queued jobs. Status polls only advance running/packaging
    # so two HTTP clients cannot double-submit to RunPod.
    if job and job.status in {"running", "packaging"}:
        job = tick_train_job(db, job)
    payload = job_to_dict(job) if job else None
    if payload:
        from helix.services.declaration import get_acceptance

        acc = get_acceptance(db, email=user.email, train_job_id=job.id)
        payload["declaration_accepted"] = bool(acc)
    return {"job": payload}


@router.get("/double-helix/train/{job_id}")
def double_helix_train_status(
    slug: str,
    job_id: str,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    _require_approved(user)
    tenant = _tenant_for(user, slug, db)
    from helix.services.double_helix_train import job_to_dict, tick_train_job

    job = db.query(m.DoubleHelixTrainJob).filter_by(id=job_id).first()
    if not job or job.tenant_id != tenant.id or job.owner_user_id != user.id:
        raise HTTPException(404, "Train job not found")
    if job.status in {"running", "packaging"}:
        job = tick_train_job(db, job)
    payload = job_to_dict(job)
    from helix.services.declaration import get_acceptance

    acc = get_acceptance(db, email=user.email, train_job_id=job.id)
    payload["declaration_accepted"] = bool(acc)
    return {"job": payload}


class DeclarationAcceptRequest(BaseModel):
    confirm: bool = False


def _client_meta(request: Request) -> tuple[str, str]:
    ip = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if not ip and request.client:
        ip = request.client.host or ""
    ua = (request.headers.get("user-agent") or "")[:500]
    return ip, ua


@router.get("/double-helix/declaration")
def double_helix_declaration_text(
    slug: str,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    _require_approved(user)
    _tenant_for(user, slug, db)
    from helix.services.declaration import declaration_payload

    return declaration_payload()


@router.get("/double-helix/declarations")
def double_helix_declaration_list(
    slug: str,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Signed copies in this account. There is no delete."""
    _require_approved(user)
    _tenant_for(user, slug, db)
    from helix.services.declaration import acceptance_to_dict, list_acceptances_for_user

    rows = list_acceptances_for_user(db, email=user.email, owner_user_id=user.id)
    return {
        "can_delete": False,
        "note": (
            "These copies stay in your account and cannot be deleted. "
            "If you delete the account, Helix still keeps them keyed to your email."
        ),
        "items": [acceptance_to_dict(r) for r in rows],
    }


@router.post("/double-helix/train/{job_id}/accept-declaration")
def double_helix_accept_declaration(
    slug: str,
    job_id: str,
    body: DeclarationAcceptRequest,
    request: Request,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    _require_approved(user)
    tenant = _tenant_for(user, slug, db)
    job = db.query(m.DoubleHelixTrainJob).filter_by(id=job_id).first()
    if not job or job.tenant_id != tenant.id or job.owner_user_id != user.id:
        raise HTTPException(404, "Train job not found")
    if job.status != "completed":
        raise HTTPException(409, "Accept the declaration only when the trained zip is ready.")
    from helix.services.declaration import accept_declaration, acceptance_to_dict

    ip, ua = _client_meta(request)
    try:
        row = accept_declaration(
            db,
            user=user,
            tenant_id=tenant.id,
            train_job_id=job.id,
            confirm=bool(body.confirm),
            ip_address=ip,
            user_agent=ua,
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {
        "ok": True,
        "acceptance": acceptance_to_dict(row),
        "download_ready": True,
    }


@router.get("/double-helix/train/{job_id}/download")
def double_helix_train_download(
    slug: str,
    job_id: str,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _require_approved(user)
    tenant = _tenant_for(user, slug, db)
    from helix.services.declaration import declaration_payload, get_acceptance
    from helix.services.double_helix_train import artifact_file

    job = db.query(m.DoubleHelixTrainJob).filter_by(id=job_id).first()
    if not job or job.tenant_id != tenant.id or job.owner_user_id != user.id:
        raise HTTPException(404, "Train job not found")
    if job.status != "completed":
        raise HTTPException(409, f"Train job is {job.status}, not ready to download.")
    acc = get_acceptance(db, email=user.email, train_job_id=job.id)
    if not acc:
        raise HTTPException(
            403,
            {
                "code": "declaration_required",
                "message": (
                    "Accept the ownership and liability declaration before "
                    "downloading the trained model."
                ),
                "declaration": declaration_payload(),
            },
        )
    path = artifact_file(job)
    if not path:
        raise HTTPException(404, "Trained zip is not on disk yet.")
    return FileResponse(
        path,
        media_type="application/zip",
        filename=f"helix_trained_{job.id}.zip",
    )
