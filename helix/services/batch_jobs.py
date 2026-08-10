"""Create and track multi-batch jobs that survive logout."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from helix.db import models as m
from helix.services.pipeline_modes import MODE_META, clamp_batch_size, clamp_mode


def _uid(prefix: str = "job_") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_batch_job(
    db: Session,
    *,
    owner_user_id: str,
    tenant_id: str,
    job_type: str,
    quality_mode: int = 2,
    batch_size: int = 5,
    total_batches: int = 1,
    auto_continue: bool = True,
    config: dict | None = None,
) -> m.BatchJob:
    if job_type not in {"pipeline", "synthesis"}:
        raise ValueError("job_type must be pipeline or synthesis")
    quality_mode = clamp_mode(quality_mode)
    batch_size = clamp_batch_size(batch_size)
    total_batches = max(1, min(int(total_batches), 500))

    # Default ETA: rough priors by mode
    prior = {1: 90.0, 2: 45.0, 3: 20.0, 4: 5.0}.get(quality_mode, 30.0)
    if job_type == "synthesis":
        prior = {1: 40.0, 2: 25.0, 3: 12.0, 4: 3.0}.get(quality_mode, 15.0)

    job = m.BatchJob(
        id=_uid(),
        owner_user_id=owner_user_id,
        tenant_id=tenant_id,
        job_type=job_type,
        quality_mode=quality_mode,
        batch_size=batch_size,
        total_batches=total_batches,
        completed_batches=0,
        auto_continue=auto_continue,
        status="pending",
        config_json=json.dumps(config or {}),
        progress_message="Queued — will run even if you sign out.",
        avg_batch_seconds=prior,
        eta_seconds=prior * total_batches,
    )
    db.add(job)
    db.add(
        m.BatchJobEvent(
            id=_uid("jbe_"),
            job_id=job.id,
            batch_index=0,
            message=f"Job queued ({job_type}, quality mode {quality_mode}, {total_batches} batches × size {batch_size}).",
            level="info",
        )
    )
    db.commit()
    db.refresh(job)
    return job


def job_to_dict(job: m.BatchJob, events: list[m.BatchJobEvent] | None = None) -> dict[str, Any]:
    remaining = max(0, job.total_batches - job.completed_batches)
    eta = job.eta_seconds
    if eta is None and job.avg_batch_seconds:
        eta = job.avg_batch_seconds * remaining
    try:
        config = json.loads(job.config_json or "{}")
    except json.JSONDecodeError:
        config = {}
    try:
        summary = json.loads(job.result_summary_json or "null")
    except json.JSONDecodeError:
        summary = None
    return {
        "id": job.id,
        "job_type": job.job_type,
        "quality_mode": job.quality_mode,
        "quality_meta": MODE_META.get(job.quality_mode, {}),
        "batch_size": job.batch_size,
        "total_batches": job.total_batches,
        "completed_batches": job.completed_batches,
        "remaining_batches": remaining,
        "auto_continue": bool(job.auto_continue),
        "status": job.status,
        "progress_message": job.progress_message,
        "items_processed": job.items_processed,
        "last_batch_seconds": job.last_batch_seconds,
        "avg_batch_seconds": job.avg_batch_seconds,
        "eta_seconds": round(eta, 1) if eta is not None else None,
        "eta_human": _human_eta(eta),
        "progress_pct": round(
            100.0 * job.completed_batches / max(job.total_batches, 1), 1
        ),
        "config": config,
        "result_summary": summary,
        "error": job.error,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "survives_logout": True,
        "events": [
            {
                "batch_index": e.batch_index,
                "message": e.message,
                "level": e.level,
                "created_at": e.created_at.isoformat() if e.created_at else None,
            }
            for e in (events or [])
        ],
    }


def _human_eta(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    s = max(0, int(seconds))
    if s < 60:
        return f"~{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"~{m}m {s}s"
    h, m = divmod(m, 60)
    return f"~{h}h {m}m"


def list_user_jobs(
    db: Session, owner_user_id: str, tenant_id: str, limit: int = 30
) -> list[dict[str, Any]]:
    rows = (
        db.query(m.BatchJob)
        .filter_by(owner_user_id=owner_user_id, tenant_id=tenant_id)
        .order_by(m.BatchJob.created_at.desc())
        .limit(limit)
        .all()
    )
    return [job_to_dict(r) for r in rows]


def get_job_detail(db: Session, job_id: str, owner_user_id: str) -> dict[str, Any] | None:
    job = db.query(m.BatchJob).filter_by(id=job_id, owner_user_id=owner_user_id).first()
    if not job:
        return None
    events = (
        db.query(m.BatchJobEvent)
        .filter_by(job_id=job.id)
        .order_by(m.BatchJobEvent.created_at.desc())
        .limit(50)
        .all()
    )
    return job_to_dict(job, list(reversed(events)))


def cancel_job(db: Session, job_id: str, owner_user_id: str) -> m.BatchJob | None:
    job = db.query(m.BatchJob).filter_by(id=job_id, owner_user_id=owner_user_id).first()
    if not job:
        return None
    if job.status in {"completed", "failed", "cancelled"}:
        return job
    job.status = "cancelled"
    job.progress_message = "Cancelled by user"
    job.finished_at = _now()
    job.updated_at = _now()
    job.eta_seconds = 0
    db.add(
        m.BatchJobEvent(
            id=_uid("jbe_"),
            job_id=job.id,
            batch_index=job.completed_batches,
            message="Job cancelled by user.",
            level="warn",
        )
    )
    db.commit()
    db.refresh(job)
    return job
