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
from helix.services.cost_tracking import should_pause_for_spend_cap
from helix.services.pipeline_modes import run_pipeline_batch
from helix.services.synthesis import run_synthesis

logger = logging.getLogger("helix.worker")

_worker_thread: threading.Thread | None = None
_stop = threading.Event()


def _uid(prefix: str = "jbe_") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _offer_riu_synthesis(db, job: m.BatchJob) -> None:
    """After gold mining, ask Riu to offer variations — never auto-start synth."""
    from helix.services.riu import _load_json, _now as riu_now, _uid as riu_uid

    row = (
        db.query(m.RiuSession)
        .filter_by(
            owner_user_id=job.owner_user_id,
            tenant_id=job.tenant_id,
            status="active",
        )
        .order_by(m.RiuSession.updated_at.desc())
        .first()
    )
    if not row:
        return
    state = _load_json(row.state_json, {})
    state["run_synthesis"] = False
    state["mining_job_id"] = job.id
    row.state_json = json.dumps(state)
    row.phase = "offer_synth"
    msgs = _load_json(row.messages_json, [])
    msgs.append(
        {
            "id": riu_uid("msg_"),
            "role": "assistant",
            "name": "Riu",
            "content": (
                "Mining finished. I did **not** start variations. "
                "If you want extra rows, say **yes** in this chat and I’ll quote "
                "the $35/1k cost first."
            ),
            "phase": "offer_synth",
            "ts": riu_now().isoformat(),
        }
    )
    row.messages_json = json.dumps(msgs)
    row.updated_at = riu_now()


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
            or_cost = float(result.get("openrouter_cost_usd") or 0.0)
            ap_cost = float(result.get("apify_cost_usd") or 0.0)
            batch_cost = float(result.get("cost_usd") or (or_cost + ap_cost))
            job.openrouter_cost_usd = float(job.openrouter_cost_usd or 0.0) + or_cost
            job.apify_cost_usd = float(job.apify_cost_usd or 0.0) + ap_cost
            job.cost_usd = float(job.cost_usd or 0.0) + batch_cost
            # Accumulate job-level gold so final status isn't last-batch-only
            try:
                prev_summary = json.loads(job.result_summary_json or "{}")
            except json.JSONDecodeError:
                prev_summary = {}
            total_gold = int(prev_summary.get("total_gold_new") or 0) + gold_new
            total_synth = int(prev_summary.get("total_synth_new") or 0)
            batch_log = list(prev_summary.get("batches") or [])
            batch_log.append(
                {
                    "batch_index": batch_index,
                    "gold_new": gold_new,
                    "openrouter_cost_usd": or_cost,
                    "apify_cost_usd": ap_cost,
                    "cost_usd": batch_cost,
                    "user_message": result.get("user_message"),
                    "elapsed_seconds": result.get("elapsed_seconds"),
                }
            )
            summary = {
                "last_batch": result,
                "quality_mode": job.quality_mode,
                "gold_new": gold_new,  # this batch only
                "total_gold_new": total_gold,  # all batches so far
                "total_synth_new": total_synth,
                "openrouter_cost_usd": job.openrouter_cost_usd,
                "apify_cost_usd": job.apify_cost_usd,
                "cost_usd": job.cost_usd,
                "spend_cap_usd": job.spend_cap_usd,
                "target_gold": job.target_gold,
                "batches": batch_log[-20:],
                "zero_evidence": bool(result.get("zero_evidence")) and total_gold == 0,
                "user_message": result.get("user_message"),
                "job_user_message": (
                    f"Job so far: {total_gold} new gold across {batch_index} batch(es). "
                    f"Cost ${job.cost_usd:.4f} "
                    f"(OR ${job.openrouter_cost_usd:.4f} + Apify ${job.apify_cost_usd:.4f}) "
                    f"/ cap ${float(job.spend_cap_usd or 0):.4f}. "
                    f"This batch: {result.get('user_message') or f'{gold_new} gold'}."
                ),
                "warnings": result.get("warnings") or [],
            }
            level = "warn" if gold_new == 0 else "info"
            _log(
                db,
                job,
                f"Batch {batch_index}/{job.total_batches} done (pipeline mode {job.quality_mode}): "
                f"gold_new={gold_new} (job total={total_gold}), units~{items}, "
                f"cost=${batch_cost:.4f} (OR ${or_cost:.4f} + Apify ${ap_cost:.4f}), "
                f"job total ${job.cost_usd:.4f}/{float(job.spend_cap_usd or 0):.4f} cap, "
                f"{result.get('elapsed_seconds')}s. "
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
            or_cost = float(result.get("openrouter_cost_usd") or result.get("cost_usd") or 0.0)
            job.openrouter_cost_usd = float(job.openrouter_cost_usd or 0.0) + or_cost
            job.cost_usd = float(job.cost_usd or 0.0) + or_cost
            try:
                prev_summary = json.loads(job.result_summary_json or "{}")
            except json.JSONDecodeError:
                prev_summary = {}
            total_synth = int(prev_summary.get("total_synth_new") or 0) + items
            total_gold = int(prev_summary.get("total_gold_new") or 0)
            summary = {
                "last_batch": result,
                "quality_mode": job.quality_mode,
                "total_gold_new": total_gold,
                "total_synth_new": total_synth,
                "openrouter_cost_usd": job.openrouter_cost_usd,
                "apify_cost_usd": job.apify_cost_usd,
                "cost_usd": job.cost_usd,
                "spend_cap_usd": job.spend_cap_usd,
                "job_user_message": (
                    f"Job so far: {total_synth} synthetic rows across "
                    f"{batch_index} batch(es); cost ${job.cost_usd:.4f}."
                ),
            }
            _log(
                db,
                job,
                f"Batch {batch_index}/{job.total_batches} done (synthesis mode {job.quality_mode}): "
                f"{items} synthetic rows (job total={total_synth}), "
                f"cost=${or_cost:.4f} (job ${job.cost_usd:.4f})"
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

        # Hard spend-cap: pause for explicit user consent (unless already overridden)
        try:
            summ_for_cap = json.loads(job.result_summary_json or "{}")
        except Exception:  # noqa: BLE001
            summ_for_cap = {}
        units_for_cap = (
            int(summ_for_cap.get("total_gold_new") or 0)
            if job.job_type == "pipeline"
            else int(summ_for_cap.get("total_synth_new") or 0)
        )
        remaining_batches = max(0, job.total_batches - job.completed_batches)
        override = bool(getattr(job, "spend_cap_override", False))
        pause, pause_msg = should_pause_for_spend_cap(
            cost_usd=float(job.cost_usd or 0.0),
            gold_new=units_for_cap,
            target_gold=int(job.target_gold or (job.batch_size * job.total_batches)),
            completed_batches=job.completed_batches,
            total_batches=job.total_batches,
        )
        # Only pause when more work remains and user has not consented past the cap.
        # If the last batch just finished, complete normally (cost already spent).
        if (
            pause
            and not override
            and remaining_batches > 0
            and job.auto_continue
        ):
            job.status = "paused_spend_cap"
            job.finished_at = None  # not terminal — awaits consent or cancel
            job.eta_seconds = 0
            consent_note = (
                f"{pause_msg} Job is paused. "
                "Confirm “Continue past cap” to resume remaining batches, "
                "or Cancel to stop."
            )
            job.progress_message = consent_note
            summ_for_cap["spend_cap_paused"] = True
            summ_for_cap["needs_spend_consent"] = True
            summ_for_cap["spend_cap_message"] = consent_note
            summ_for_cap["openrouter_cost_usd"] = job.openrouter_cost_usd
            summ_for_cap["apify_cost_usd"] = job.apify_cost_usd
            summ_for_cap["cost_usd"] = job.cost_usd
            summ_for_cap["job_user_message"] = consent_note
            job.result_summary_json = json.dumps(summ_for_cap, default=str)
            _log(db, job, consent_note, level="warn")
            db.commit()
            return

        if job.completed_batches >= job.total_batches or not job.auto_continue:
            job.status = "completed"
            job.finished_at = _now()
            job.eta_seconds = 0
            try:
                summ = json.loads(job.result_summary_json or "{}")
            except Exception:  # noqa: BLE001
                summ = {}
            total_gold = int(summ.get("total_gold_new") or 0)
            total_synth = int(summ.get("total_synth_new") or 0)
            cost_note = (
                f" Cost ${float(job.cost_usd or 0):.4f} "
                f"(OpenRouter ${float(job.openrouter_cost_usd or 0):.4f} + "
                f"Apify ${float(job.apify_cost_usd or 0):.4f})"
                f" / cap ${float(job.spend_cap_usd or 0):.4f}."
            )
            if job.job_type == "pipeline":
                if total_gold > 0:
                    job.progress_message = (
                        f"Completed {job.completed_batches}/{job.total_batches} batches — "
                        f"{total_gold} new gold example(s) saved to your library."
                        + cost_note
                    )
                else:
                    job.progress_message = (
                        f"Completed {job.completed_batches}/{job.total_batches} batches — "
                        f"0 new gold examples "
                        f"(last batch: {summ.get('user_message') or 'no new rows'})."
                        + cost_note
                    )
            else:
                job.progress_message = (
                    f"Completed {job.completed_batches}/{job.total_batches} batches — "
                    f"{total_synth} synthetic row(s) saved."
                    + cost_note
                )
            # Keep cumulative totals on the summary for the UI
            summ["job_user_message"] = job.progress_message
            summ["total_gold_new"] = total_gold
            summ["total_synth_new"] = total_synth
            summ["openrouter_cost_usd"] = job.openrouter_cost_usd
            summ["apify_cost_usd"] = job.apify_cost_usd
            summ["cost_usd"] = job.cost_usd
            job.result_summary_json = json.dumps(summ, default=str)
            _log(db, job, job.progress_message, level="info")
            if job.job_type == "pipeline":
                try:
                    _offer_riu_synthesis(db, job)
                except Exception:  # noqa: BLE001
                    logger.exception("riu offer_synth hook failed")
        else:
            try:
                summ = json.loads(job.result_summary_json or "{}")
                total_gold = int(summ.get("total_gold_new") or 0)
            except Exception:  # noqa: BLE001
                total_gold = 0
            job.progress_message = (
                f"Finished batch {job.completed_batches}/{job.total_batches} "
                f"({total_gold} gold so far; "
                f"${float(job.cost_usd or 0):.4f}/"
                f"${float(job.spend_cap_usd or 0):.4f} cap). "
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
