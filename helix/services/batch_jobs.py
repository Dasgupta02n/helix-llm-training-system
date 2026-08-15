"""Create and track multi-batch jobs that survive logout."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from helix.db import models as m
from helix.services.cost_tracking import (
    format_row_rate,
    gold_spend_cap_usd,
    usage_from_provider_parts,
)
from helix.services.pipeline_modes import (
    MODE_META,
    clamp_batch_size,
    clamp_mode,
    eta_prior_seconds,
)


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
    no_corpus: bool = False,
) -> m.BatchJob:
    if job_type not in {"pipeline", "synthesis"}:
        raise ValueError("job_type must be pipeline or synthesis")
    quality_mode = clamp_mode(quality_mode)
    batch_size = clamp_batch_size(batch_size)
    total_batches = max(1, min(int(total_batches), 100))

    # Conservative priors (recalibrated ~2–3× old heuristics from live runs)
    prior = eta_prior_seconds(job_type, quality_mode)
    # Prefer historical avg for this user/tenant if available
    hist = (
        db.query(m.BatchJob)
        .filter(
            m.BatchJob.owner_user_id == owner_user_id,
            m.BatchJob.tenant_id == tenant_id,
            m.BatchJob.job_type == job_type,
            m.BatchJob.quality_mode == quality_mode,
            m.BatchJob.status == "completed",
            m.BatchJob.avg_batch_seconds > 0,
        )
        .order_by(m.BatchJob.finished_at.desc())
        .limit(5)
        .all()
    )
    if hist:
        prior = sum(j.avg_batch_seconds for j in hist) / len(hist)

    # Spend-cap target: pipeline aims for ~batch_size gold per batch.
    # Synthesis uses synthetic rows as scale (not gold) — still cap on cost trajectory
    # using batch_size * total_batches as the job's "unit" target.
    target_gold = max(1, batch_size * total_batches)
    cfg = dict(config or {})
    no_corpus = bool(no_corpus or cfg.get("no_corpus_rate"))
    kind = "synthetic" if job_type == "synthesis" else "gold"
    spend_cap = gold_spend_cap_usd(target_gold, no_corpus=no_corpus, kind=kind)
    rate_note = format_row_rate(no_corpus=no_corpus, kind=kind)

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
        target_gold=target_gold,
        spend_cap_usd=spend_cap,
        spend_cap_override=False,
        openrouter_cost_usd=0.0,
        apify_cost_usd=0.0,
        compute_cost_usd=0.0,
        cost_usd=0.0,
    )
    db.add(job)
    db.add(
        m.BatchJobEvent(
            id=_uid("jbe_"),
            job_id=job.id,
            batch_index=0,
            message=(
                f"Job queued ({job_type}, quality mode {quality_mode}, "
                f"{total_batches} batches × size {batch_size}). "
                f"Spend cap ${spend_cap:.4f} for {target_gold} target units "
                f"({rate_note})."
            ),
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
    from helix.services.live_status import heartbeat_fields, public_activity_text

    hb = heartbeat_fields(
        job.updated_at,
        running=job.status in {"pending", "running"},
        started_at=job.started_at,
    )
    if isinstance(summary, dict):
        summary = dict(summary)
        for key in ("job_user_message", "user_message", "spend_cap_message"):
            if key in summary:
                summary[key] = public_activity_text(summary.get(key))
        last = summary.get("last_batch")
        if isinstance(last, dict):
            last = dict(last)
            if "user_message" in last:
                last["user_message"] = public_activity_text(last.get("user_message"))
            summary["last_batch"] = last
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
        "progress_message": public_activity_text(job.progress_message),
        "items_processed": job.items_processed,
        "last_batch_seconds": job.last_batch_seconds,
        "avg_batch_seconds": job.avg_batch_seconds,
        "eta_seconds": round(eta, 1) if eta is not None else None,
        "eta_human": _human_eta(eta),
        "progress_pct": round(
            100.0 * job.completed_batches / max(job.total_batches, 1), 1
        ),
        **(
            lambda usage: {
                "openrouter_cost_usd": usage["model_usd"],
                "apify_cost_usd": usage["gather_usd"],
                "compute_cost_usd": usage["compute_usd"],
                "provider_cost_usd": usage["provider_usd"],
                "user_charge_usd": usage["user_charge_usd"],
                "cost_usd": usage["user_charge_usd"],
                "markup": usage["markup"],
                "cost_breakdown": {
                    "provider_usd": usage["provider_usd"],
                    "user_charge_usd": usage["user_charge_usd"],
                    "markup": usage["markup"],
                    "model_usd": usage["model_usd"],
                    "gather_usd": usage["gather_usd"],
                    "compute_usd": usage["compute_usd"],
                    "total_usd": usage["user_charge_usd"],
                    "spend_cap_usd": round(float(job.spend_cap_usd or 0.0), 6),
                    "target_gold": int(job.target_gold or 0),
                    "spend_cap_override": bool(
                        getattr(job, "spend_cap_override", False)
                    ),
                },
            }
        )(
            usage_from_provider_parts(
                model_usd=float(job.openrouter_cost_usd or 0.0),
                gather_usd=float(job.apify_cost_usd or 0.0),
                compute_usd=float(getattr(job, "compute_cost_usd", 0.0) or 0.0),
            )
        ),
        "target_gold": int(job.target_gold or 0),
        "spend_cap_usd": round(float(job.spend_cap_usd or 0.0), 6),
        "spend_cap_override": bool(getattr(job, "spend_cap_override", False)),
        "needs_spend_consent": (job.status == "paused_spend_cap")
        and not bool(getattr(job, "spend_cap_override", False)),
        "config": config,
        "result_summary": summary,
        "error": public_activity_text(job.error) or job.error,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "survives_logout": True,
        **hb,
        "events": [
            {
                "batch_index": e.batch_index,
                "message": public_activity_text(e.message),
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
    ids = [r.id for r in rows]
    by_job: dict[str, list] = {i: [] for i in ids}
    if ids:
        evs = (
            db.query(m.BatchJobEvent)
            .filter(m.BatchJobEvent.job_id.in_(ids))
            .order_by(m.BatchJobEvent.created_at.desc())
            .limit(max(24 * len(ids), 24))
            .all()
        )
        for e in evs:
            bucket = by_job.get(e.job_id)
            if bucket is not None and len(bucket) < 16:
                bucket.append(e)
    out = []
    for r in rows:
        out.append(job_to_dict(r, list(reversed(by_job.get(r.id) or []))))
    return out


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
    # Allow cancel from paused_spend_cap (user declines to continue past cap)
    was_paused = job.status == "paused_spend_cap"
    job.status = "cancelled"
    job.progress_message = (
        "Cancelled by user (did not continue past spend cap)."
        if was_paused
        else "Cancelled by user"
    )
    job.finished_at = _now()
    job.updated_at = _now()
    job.eta_seconds = 0
    db.add(
        m.BatchJobEvent(
            id=_uid("jbe_"),
            job_id=job.id,
            batch_index=job.completed_batches,
            message=job.progress_message,
            level="warn",
        )
    )
    db.commit()
    db.refresh(job)
    return job


def continue_past_spend_cap(
    db: Session, job_id: str, owner_user_id: str
) -> m.BatchJob | None:
    """
    Explicit user consent to resume a job paused for exceeding the per-row
    gold spend trajectory. Without this, the job stays paused.
    """
    job = db.query(m.BatchJob).filter_by(id=job_id, owner_user_id=owner_user_id).first()
    if not job:
        return None
    if job.status != "paused_spend_cap":
        # Already running / done / cancelled — return as-is (idempotent for clients)
        return job

    remaining = max(0, int(job.total_batches) - int(job.completed_batches))
    job.spend_cap_override = True
    job.finished_at = None
    job.updated_at = _now()
    job.error = None

    try:
        summary = json.loads(job.result_summary_json or "{}")
    except json.JSONDecodeError:
        summary = {}
    summary["spend_cap_paused"] = False
    summary["spend_cap_override"] = True
    summary["spend_cap_consent_at"] = _now().isoformat()
    summary["spend_cap_message"] = (
        "User consented to continue past spend cap "
        f"(${float(job.cost_usd or 0):.4f} spent / "
        f"cap ${float(job.spend_cap_usd or 0):.4f})."
    )

    if remaining <= 0 or not job.auto_continue:
        # All batches already finished when cap fired — mark completed with consent note
        job.status = "completed"
        job.eta_seconds = 0
        job.progress_message = (
            f"Completed with user consent after spend-cap pause. "
            f"Cost ${float(job.cost_usd or 0):.4f} "
            f"(cap was ${float(job.spend_cap_usd or 0):.4f})."
        )
        summary["job_user_message"] = job.progress_message
        job.result_summary_json = json.dumps(summary, default=str)
        job.finished_at = _now()
        db.add(
            m.BatchJobEvent(
                id=_uid("jbe_"),
                job_id=job.id,
                batch_index=job.completed_batches,
                message=(
                    "User consented past spend cap; no remaining batches — marked completed."
                ),
                level="info",
            )
        )
    else:
        job.status = "pending"
        job.progress_message = (
            f"Resumed after spend-cap consent. "
            f"{remaining} batch(es) remaining. Cap override active "
            f"(${float(job.cost_usd or 0):.4f} already spent / "
            f"original cap ${float(job.spend_cap_usd or 0):.4f})."
        )
        summary["job_user_message"] = job.progress_message
        job.result_summary_json = json.dumps(summary, default=str)
        remaining_eta = (job.avg_batch_seconds or 0) * remaining
        job.eta_seconds = round(remaining_eta, 1) if remaining_eta else None
        db.add(
            m.BatchJobEvent(
                id=_uid("jbe_"),
                job_id=job.id,
                batch_index=job.completed_batches,
                message=(
                    "User consented to continue past spend cap. "
                    f"Re-queued {remaining} remaining batch(es) with override."
                ),
                level="warn",
            )
        )
    db.commit()
    db.refresh(job)
    return job
