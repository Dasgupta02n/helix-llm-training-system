"""Batch job API — multi-batch pipeline & synthesis that survive logout."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from helix.api.deps import get_current_user
from helix.db import models as m
from helix.db.session import get_db
from helix.services.batch_jobs import (
    cancel_job,
    continue_past_spend_cap,
    create_batch_job,
    get_job_detail,
    job_to_dict,
    list_user_jobs,
)
from helix.services.pipeline_modes import MODE_META

router = APIRouter(prefix="/api/t/{slug}/jobs", tags=["jobs"])


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
            raise HTTPException(403, "Forbidden")
    return tenant


class PipelineJobCreate(BaseModel):
    quality_mode: int = Field(2, ge=1, le=4, description="1=best quality, 4=lowest cost")
    batch_size: int = Field(5, ge=1, le=10)
    total_batches: int = Field(1, ge=1, le=500)
    auto_continue: bool = True


class SynthesisJobCreate(BaseModel):
    quality_mode: int = Field(2, ge=1, le=4)
    batch_size: int = Field(5, ge=1, le=10, description="Gold rows per batch")
    total_batches: int = Field(1, ge=1, le=500)
    auto_continue: bool = True
    variations_per_gold: int = Field(4, ge=1, le=20)
    parameters: list[str] = Field(default_factory=lambda: ["tone", "difficulty", "persona"])


@router.get("/modes")
def list_modes() -> dict:
    return {
        "slider": {
            "min": 1,
            "max": 4,
            "best_quality": 1,
            "lowest_cost": 4,
            "labels": {
                "1": "Best quality — all 15 agents",
                "2": "High quality — core AI helpers",
                "3": "Balanced — quality gates only",
                "4": "Lowest cost — ultra lean (code/templates)",
            },
        },
        "modes": MODE_META,
        "batch_size_max": 10,
        "survives_logout": True,
    }


@router.post("/pipeline")
def start_pipeline_job(
    slug: str,
    body: PipelineJobCreate,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    tenant = _tenant_for(user, slug, db)
    job = create_batch_job(
        db,
        owner_user_id=user.id,
        tenant_id=tenant.id,
        job_type="pipeline",
        quality_mode=body.quality_mode,
        batch_size=body.batch_size,
        total_batches=body.total_batches,
        auto_continue=body.auto_continue,
        config={},
    )
    return {
        "ok": True,
        "message": "Mining job queued. It keeps running if you sign out.",
        "job": job_to_dict(job),
    }


@router.post("/synthesis")
def start_synthesis_job(
    slug: str,
    body: SynthesisJobCreate,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    tenant = _tenant_for(user, slug, db)
    job = create_batch_job(
        db,
        owner_user_id=user.id,
        tenant_id=tenant.id,
        job_type="synthesis",
        quality_mode=body.quality_mode,
        batch_size=body.batch_size,
        total_batches=body.total_batches,
        auto_continue=body.auto_continue,
        config={
            "variations_per_gold": body.variations_per_gold,
            "parameters": body.parameters,
        },
    )
    return {
        "ok": True,
        "message": "Synthesis job queued. It keeps running if you sign out.",
        "job": job_to_dict(job),
    }


@router.get("")
def list_jobs(
    slug: str,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    tenant = _tenant_for(user, slug, db)
    jobs = list_user_jobs(db, user.id, tenant.id)
    active = [j for j in jobs if j["status"] in {"pending", "running"}]
    return {"jobs": jobs, "active": active}


@router.get("/{job_id}")
def get_job(
    slug: str,
    job_id: str,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    _tenant_for(user, slug, db)
    detail = get_job_detail(db, job_id, user.id)
    if not detail:
        raise HTTPException(404, "Job not found")
    return detail


@router.post("/{job_id}/cancel")
def cancel(
    slug: str,
    job_id: str,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    _tenant_for(user, slug, db)
    job = cancel_job(db, job_id, user.id)
    if not job:
        raise HTTPException(404, "Job not found")
    return {"ok": True, "job": job_to_dict(job)}


class SpendCapContinueBody(BaseModel):
    """Explicit consent required — prevents accidental resume past the cap."""

    confirm: bool = Field(
        False,
        description="Must be true to continue past the $35/1k gold spend cap.",
    )


@router.post("/{job_id}/continue-past-cap")
def continue_past_cap(
    slug: str,
    job_id: str,
    body: SpendCapContinueBody | None = None,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """
    Resume a job paused for spend-cap only after explicit user confirmation.
    Without confirm=true the job stays paused.
    """
    _tenant_for(user, slug, db)
    body = body or SpendCapContinueBody()
    if not body.confirm:
        raise HTTPException(
            400,
            "confirm=true is required to continue past the spend cap. "
            "Job remains paused until you confirm or cancel.",
        )
    job = db.query(m.BatchJob).filter_by(id=job_id, owner_user_id=user.id).first()
    if not job:
        raise HTTPException(404, "Job not found")
    if job.status != "paused_spend_cap":
        # Idempotent: already running/done
        return {
            "ok": True,
            "resumed": False,
            "message": f"Job is {job.status} (not awaiting spend-cap consent).",
            "job": job_to_dict(job),
        }
    job = continue_past_spend_cap(db, job_id, user.id)
    if not job:
        raise HTTPException(404, "Job not found")
    return {
        "ok": True,
        "resumed": job.status in {"pending", "running", "completed"},
        "message": (
            "Spend-cap consent recorded. Job will continue remaining batches."
            if job.status in {"pending", "running"}
            else job.progress_message
        ),
        "job": job_to_dict(job),
    }
