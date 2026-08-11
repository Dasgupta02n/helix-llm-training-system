"""Background worker: runs queued batch jobs after users log out.

Started on API startup. Polls batch_jobs table and processes one batch at a time.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from datetime import datetime, timezone

from helix.db.session import SessionLocal
from helix.db import models as m
from helix.services.pipeline_modes import run_pipeline_batch
from helix.services.synthesis import run_synthesis

logger = logging.getLogger("helix.worker")

_worker_thread: threading.Thread | None = None
_stop = threading.Event()


def _uid(prefix: str = "jbe_") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _log(db, job: m.BatchJob, message: str, level: str = "info") -> None:
    db.add(
        m.BatchJobEvent(
            id=_uid(),
            job_id=job.id,
            batch_index=job.completed_batches + 1,
            message=message,
            level=level,
        )
    )


def _pick_job(db) -> m.BatchJob | None:
    # Prefer already-running (resume), then oldest pending
    running = (
        db.query(m.BatchJob)
        .filter(m.BatchJob.status == "running")
        .order_by(m.BatchJob.updated_at.asc())
        .first()
    )
    if running:
        return running
    return (
        db.query(m.BatchJob)
        .filter(m.BatchJob.status == "pending")
        .order_by(m.BatchJob.created_at.asc())
        .first()
    )


def _process_one_batch(db, job: m.BatchJob) -> None:
    if job.status == "cancelled":
        return

    if job.started_at is None:
        job.started_at = _now()
    job.status = "running"
    job.progress_message = (
        f"Running batch {job.completed_batches + 1} of {job.total_batches}…"
    )
    job.updated_at = _now()
    db.commit()

    batch_index = job.completed_batches + 1
    t0 = time.time()
    try:
        config = json.loads(job.config_json or "{}")
    except json.JSONDecodeError:
        config = {}

    try:
        if job.job_type == "pipeline":
            def _progress(msg: str) -> None:
                job.progress_message = (
                    f"Batch {batch_index}/{job.total_batches}: {msg}"
                )
                job.updated_at = _now()
                try:
                    db.commit()
                except Exception:  # noqa: BLE001
                    db.rollback()

            result = run_pipeline_batch(
                db,
                tenant_id=job.tenant_id,
                owner_user_id=job.owner_user_id,
                quality_mode=job.quality_mode,
                batch_size=job.batch_size,
                progress_cb=_progress,
            )
            items = int(result.get("items_processed") or 0)
            gold_new = int(result.get("gold_new") or 0)
            summary = {
                "last_batch": result,
                "quality_mode": job.quality_mode,
                "gold_new": gold_new,
                "zero_evidence": bool(result.get("zero_evidence")),
                "user_message": result.get("user_message"),
                "warnings": result.get("warnings") or [],
            }
            level = "warn" if result.get("zero_evidence") or gold_new == 0 else "info"
            _log(
                db,
                job,
                f"Batch {batch_index}/{job.total_batches} done (pipeline mode {job.quality_mode}): "
                f"gold_new={gold_new}, units~{items}, {result.get('elapsed_seconds')}s. "
                f"{result.get('user_message') or ''}",
                level=level,
            )
        else:
            # synthesis: modes 1–2 use LLM when available; 3–4 use templates (low tokens)
            use_llm = job.quality_mode in (1, 2)
            vars_per = int(config.get("variations_per_gold") or 4)
            result = run_synthesis(
                db,
                owner_user_id=job.owner_user_id,
                tenant_id=job.tenant_id,
                variations_per_gold=vars_per,
                parameters=config.get("parameters"),
                max_golds=job.batch_size,
                use_llm=use_llm,
            )
            items = int(result.get("synthesized_count") or 0)
            summary = {"last_batch": result, "quality_mode": job.quality_mode}
            _log(
                db,
                job,
                f"Batch {batch_index}/{job.total_batches} done (synthesis mode {job.quality_mode}): "
                f"{items} synthetic rows"
                + ("" if result.get("ok") else f" — {result.get('message')}"),
                level="info" if result.get("ok") else "warn",
            )

        elapsed = time.time() - t0
        job.completed_batches += 1
        job.items_processed += items
        job.last_batch_seconds = round(elapsed, 2)
        # running average
        n = job.completed_batches
        job.avg_batch_seconds = round(
            ((job.avg_batch_seconds * (n - 1)) + elapsed) / n if n else elapsed, 2
        )
        remaining = max(0, job.total_batches - job.completed_batches)
        job.eta_seconds = round(job.avg_batch_seconds * remaining, 1)
        job.result_summary_json = json.dumps(summary, default=str)
        job.updated_at = _now()

        if job.status == "cancelled":
            db.commit()
            return

        if job.completed_batches >= job.total_batches or not job.auto_continue:
            job.status = "completed"
            job.finished_at = _now()
            job.eta_seconds = 0
            # Prefer last batch user_message when available
            try:
                summ = json.loads(job.result_summary_json or "{}")
                um = (summ.get("user_message") or summ.get("last_batch", {}).get("user_message"))
            except Exception:  # noqa: BLE001
                um = None
            job.progress_message = um or (
                f"Completed {job.completed_batches}/{job.total_batches} batches. "
                f"{job.items_processed} items processed. Data is in your account."
            )
            _log(db, job, job.progress_message, level="info")
        else:
            job.progress_message = (
                f"Finished batch {job.completed_batches}/{job.total_batches}. "
                f"ETA ~{int(job.eta_seconds or 0)}s. Continuing automatically…"
            )
            job.status = "pending"  # re-queue next batch
        db.commit()
    except Exception as e:  # noqa: BLE001
        logger.exception("Batch job %s failed", job.id)
        job.status = "failed"
        job.error = str(e)
        job.progress_message = f"Failed: {e}"
        job.finished_at = _now()
        job.updated_at = _now()
        _log(db, job, f"Error: {e}", level="error")
        db.commit()


def worker_loop(poll_seconds: float = 2.0) -> None:
    logger.info("Helix batch worker started")
    while not _stop.is_set():
        db = SessionLocal()
        try:
            job = _pick_job(db)
            if not job:
                db.close()
                _stop.wait(poll_seconds)
                continue
            # Re-check cancelled
            db.refresh(job)
            if job.status == "cancelled":
                db.close()
                continue
            _process_one_batch(db, job)
        except Exception:  # noqa: BLE001
            logger.exception("Worker loop error")
            try:
                db.rollback()
            except Exception:  # noqa: BLE001
                pass
        finally:
            try:
                db.close()
            except Exception:  # noqa: BLE001
                pass
        _stop.wait(0.5)
    logger.info("Helix batch worker stopped")


def start_worker() -> None:
    global _worker_thread
    if _worker_thread and _worker_thread.is_alive():
        return
    _stop.clear()
    _worker_thread = threading.Thread(
        target=worker_loop, name="helix-batch-worker", daemon=True
    )
    _worker_thread.start()


def stop_worker() -> None:
    _stop.set()
