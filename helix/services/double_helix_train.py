"""Double Helix train: pull gold from the user's Helix account → RunPod QLoRA → zip.

Option A (unchanged): user downloads gold and trains anywhere.
Option B (this module): Helix fetches that same gold, trains on Serverless,
then offers a zip of the QLoRA adapter + tokenizer + notes. Full merged
7B–30B weights are not included (tens of GB).
"""

from __future__ import annotations

import json
import shutil
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from helix.config import DATA_DIR
from helix.db import models as m
from helix.services.base_models import get_model, recommend_model
from helix.services.double_helix import LICENSE_NOTE, USAGE_OWNERSHIP
from helix.services.hf_hub_io import (
    ADAPTER_NAMES,
    TOKENIZER_NAMES,
    collect_named_files,
    create_private_repo,
    download_repo_files,
    upload_text_files,
)
from helix.services.library import gold_to_chat_messages
from helix.services.live_status import heartbeat_fields, public_activity_text
from helix.services.runpod_train import (
    poll_qlora_job,
    submit_qlora_job,
    train_ready,
)

ACTIVE_STATUSES = frozenset({"queued", "uploading", "running", "packaging"})
ESTIMATE_USD_MIN = 15
ESTIMATE_USD_MAX = 50


def _uid() -> str:
    return f"dht_{uuid.uuid4().hex[:12]}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def artifacts_dir() -> Path:
    d = DATA_DIR / "double_helix_artifacts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_trainable_gold(
    db: Session, *, owner_user_id: str, tenant_id: str
) -> list[dict[str, Any]]:
    rows = (
        db.query(m.GoldExample)
        .filter_by(
            owner_user_id=owner_user_id,
            tenant_id=tenant_id,
            is_archived=False,
        )
        .filter(m.GoldExample.verification_status != "rejected")
        .order_by(m.GoldExample.created_at.asc())
        .all()
    )
    return [
        {
            "id": g.id,
            "input": g.input_text or "",
            "output": g.output_text or "",
        }
        for g in rows
        if (g.input_text or "").strip() and (g.output_text or "").strip()
    ]


def gold_to_alpaca_line(row: dict[str, Any]) -> dict[str, str]:
    return {
        "instruction": row.get("input") or "",
        "input": "",
        "output": row.get("output") or "",
    }


def build_dataset_texts(rows: list[dict[str, Any]]) -> tuple[str, str]:
    chat_lines = [
        json.dumps(gold_to_chat_messages(r["input"], r["output"]), ensure_ascii=False)
        for r in rows
    ]
    alpaca_lines = [json.dumps(gold_to_alpaca_line(r), ensure_ascii=False) for r in rows]
    nl = "\n"
    return nl.join(chat_lines) + (nl if chat_lines else ""), nl.join(alpaca_lines) + (
        nl if alpaca_lines else ""
    )


def _activity(job: m.DoubleHelixTrainJob) -> list[dict[str, Any]]:
    try:
        meta = json.loads(job.meta_json or "{}")
    except json.JSONDecodeError:
        meta = {}
    evs = meta.get("activity") or []
    return evs if isinstance(evs, list) else []


def append_train_activity(job: m.DoubleHelixTrainJob, message: str, *, level: str = "info") -> None:
    meta: dict[str, Any]
    try:
        meta = json.loads(job.meta_json or "{}")
    except json.JSONDecodeError:
        meta = {}
    clean = public_activity_text(message) or (message or "").strip()
    evs = list(meta.get("activity") or [])
    evs.append(
        {
            "message": clean,
            "level": level,
            "created_at": _now().isoformat(),
        }
    )
    meta["activity"] = evs[-40:]
    job.meta_json = json.dumps(meta)
    job.progress_message = clean[:500]
    job.updated_at = _now()


def job_to_dict(job: m.DoubleHelixTrainJob) -> dict[str, Any]:
    download_ready = bool(job.status == "completed" and job.artifact_relpath)
    running = job.status in ACTIVE_STATUSES
    hb = heartbeat_fields(
        job.updated_at, running=running, started_at=job.created_at
    )
    return {
        "id": job.id,
        "status": job.status,
        "base_model_id": job.base_model_id,
        "gold_count": job.gold_count,
        "progress": public_activity_text(job.progress_message),
        "error": public_activity_text(job.error) or job.error,
        "download_ready": download_ready,
        "hf_dataset_repo": job.hf_dataset_repo,
        "hf_model_repo": job.hf_model_repo,
        "estimated_usd_min": ESTIMATE_USD_MIN,
        "estimated_usd_max": ESTIMATE_USD_MAX,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "updated_at": job.updated_at.isoformat() if job.updated_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "events": [
            {
                **e,
                "message": public_activity_text(e.get("message") if isinstance(e, dict) else ""),
            }
            for e in _activity(job)[-16:]
            if isinstance(e, dict)
        ],
        **hb,
    }


def active_train_job(
    db: Session, *, owner_user_id: str, tenant_id: str
) -> m.DoubleHelixTrainJob | None:
    return (
        db.query(m.DoubleHelixTrainJob)
        .filter(
            m.DoubleHelixTrainJob.owner_user_id == owner_user_id,
            m.DoubleHelixTrainJob.tenant_id == tenant_id,
            m.DoubleHelixTrainJob.status.in_(ACTIVE_STATUSES),
        )
        .order_by(m.DoubleHelixTrainJob.created_at.desc())
        .first()
    )


def latest_train_job(
    db: Session, *, owner_user_id: str, tenant_id: str
) -> m.DoubleHelixTrainJob | None:
    return (
        db.query(m.DoubleHelixTrainJob)
        .filter_by(owner_user_id=owner_user_id, tenant_id=tenant_id)
        .order_by(m.DoubleHelixTrainJob.created_at.desc())
        .first()
    )


def create_train_job(
    db: Session,
    *,
    owner_user_id: str,
    tenant_id: str,
    model_id: str | None,
    confirm: bool,
) -> m.DoubleHelixTrainJob:
    if not confirm:
        raise ValueError(
            "Training starts a paid GPU job (~$15–50). "
            "Send confirm=true after you accept that."
        )
    if not train_ready():
        raise ValueError(
            "Double Helix train is not ready on this server."
        )
    existing = (
        db.query(m.DoubleHelixTrainJob)
        .filter(
            m.DoubleHelixTrainJob.owner_user_id == owner_user_id,
            m.DoubleHelixTrainJob.tenant_id == tenant_id,
            m.DoubleHelixTrainJob.status.in_(ACTIVE_STATUSES),
        )
        .order_by(m.DoubleHelixTrainJob.created_at.desc())
        .with_for_update()
        .first()
    )
    if existing:
        raise ValueError(
            f"A Double Helix train is already {existing.status} ({existing.id}). "
            "Wait for it to finish or cancel it."
        )
    rows = load_trainable_gold(db, owner_user_id=owner_user_id, tenant_id=tenant_id)
    if not rows:
        raise ValueError(
            "No trainable gold in this account. Mine, upload, or convert materials first. "
            "You can still download an empty package, but training needs examples."
        )
    model = get_model(model_id or "") or recommend_model()
    job = m.DoubleHelixTrainJob(
        id=_uid(),
        owner_user_id=owner_user_id,
        tenant_id=tenant_id,
        status="queued",
        base_model_id=model["id"],
        gold_count=len(rows),
        progress_message=(
            f"Queued QLoRA on {model['name']} using {len(rows)} gold row(s) "
            "already in your Helix account."
        ),
        meta_json=json.dumps(
            {
                "base_model_name": model["name"],
                "license": model["license"],
                "params_b": model["params_b"],
                "activity": [
                    {
                        "message": (
                            f"Queued QLoRA on {model['name']} using {len(rows)} "
                            "gold row(s) from this account."
                        ),
                        "level": "info",
                        "created_at": _now().isoformat(),
                    }
                ],
            }
        ),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _fail(job: m.DoubleHelixTrainJob, msg: str) -> None:
    job.status = "failed"
    job.error = msg[:2000]
    append_train_activity(job, msg[:500], level="error")
    job.finished_at = _now()


def _hf_token() -> str:
    from helix.config import get_settings

    return (getattr(get_settings(), "hf_token", "") or "").strip()


def cancel_train_job(
    db: Session, *, job: m.DoubleHelixTrainJob
) -> m.DoubleHelixTrainJob:
    if job.status in {"completed", "failed", "cancelled"}:
        raise ValueError(f"Job is already {job.status}.")
    job.status = "cancelled"
    append_train_activity(job, "Cancelled. No further training submit or packaging.")
    job.finished_at = _now()
    db.commit()
    db.refresh(job)
    return job


def _advance_queued(db: Session, job: m.DoubleHelixTrainJob) -> None:
    if job.runpod_job_id:
        job.status = "running"
        append_train_activity(job, "Resuming watch on the existing training job.")
        return
    token = _hf_token()
    rows = load_trainable_gold(
        db, owner_user_id=job.owner_user_id, tenant_id=job.tenant_id
    )
    if not rows:
        _fail(job, "Gold disappeared from the account before training started.")
        return
    job.gold_count = len(rows)
    job.status = "uploading"
    append_train_activity(
        job, f"Uploading {len(rows)} gold row(s) from your Helix account…"
    )
    db.commit()

    slug = job.id.replace("_", "-")
    ds_name = f"helix-gold-{slug}"
    md_name = f"helix-qlora-{slug}"
    ds_repo = create_private_repo(token, name=ds_name, repo_type="dataset")
    md_repo = create_private_repo(token, name=md_name, repo_type="model")
    chat_text, alpaca_text = build_dataset_texts(rows)
    upload_text_files(
        token,
        repo_id=ds_repo,
        repo_type="dataset",
        files={
            "train_chat.jsonl": chat_text,
            "train_alpaca.jsonl": alpaca_text,
            "README.md": (
                f"# Helix gold for `{job.base_model_id}`\n\n"
                f"{len(rows)} chat/alpaca rows exported from a Helix account. Private.\n"
            ),
        },
    )
    job.hf_dataset_repo = ds_repo
    job.hf_model_repo = md_repo
    append_train_activity(job, "Gold uploaded. Submitting the training job…")
    db.commit()

    submitted = submit_qlora_job(
        model_id=job.base_model_id,
        dataset_repo=ds_repo,
        hub_model_id=md_repo,
        run_id=job.id,
        gold_count=len(rows),
        hf_token=token,
    )
    if not submitted.get("ok"):
        _fail(job, submitted.get("error") or "Could not start the training job.")
        return
    rp_id = submitted.get("runpod_job_id") or ""
    if not rp_id and isinstance(submitted.get("runpod"), dict):
        rp_id = submitted["runpod"].get("id") or ""
    if not rp_id:
        _fail(job, "Training was accepted but no job id came back.")
        return
    job.runpod_job_id = str(rp_id)
    job.status = "running"
    append_train_activity(
        job,
        "Training job queued (pay per run, idle when unused). "
        "Usually 20–90 minutes depending on model size.",
    )
    db.commit()


def _advance_running(job: m.DoubleHelixTrainJob) -> None:
    polled = poll_qlora_job(job.runpod_job_id or "")
    if not polled.get("ok"):
        append_train_activity(
            job, f"Checking training status… ({polled.get('error') or 'retry'})"
        )
        return
    st = str(polled.get("status") or "").upper()
    if st in {"COMPLETED", "COMPLETE", "SUCCESS"}:
        job.status = "packaging"
        append_train_activity(job, "Training finished. Packaging adapter + tokenizer…")
        return
    if st in {"FAILED", "CANCELLED", "CANCELED", "TIMED_OUT", "TIMEOUT"}:
        _fail(job, f"Training job {st}. Check the training logs for the error.")
        return
    pretty = st.replace("_", " ").title() or "Queued"
    evs = _activity(job)
    last = (evs[-1]["message"] if evs else "") or ""
    same = (
        f"Training status: {pretty}" in last
        or f"RunPod status: {pretty}" in last
        or f"training status: {pretty}" in last
    )
    job.updated_at = _now()
    if not same:
        append_train_activity(job, f"Training status: {pretty} (still training).")
    else:
        # Heartbeat only — UI can see updated_at move without a new log line every poll.
        try:
            last_ts = evs[-1].get("created_at") if evs else None
            if last_ts:
                prev = datetime.fromisoformat(str(last_ts).replace("Z", "+00:00"))
                if (_now() - prev).total_seconds() >= 20:
                    append_train_activity(job, f"Heartbeat — still {pretty}.")
        except Exception:  # noqa: BLE001
            pass


def build_trained_zip(
    *,
    job: m.DoubleHelixTrainJob,
    adapter_dir: Path,
    tokenizer_dir: Path | None,
    gold_rows: list[dict[str, Any]],
) -> bytes:
    import io

    model = get_model(job.base_model_id) or {"id": job.base_model_id, "name": job.base_model_id, "license": "", "params_b": ""}
    chat_text, _alpaca = build_dataset_texts(gold_rows)
    from helix.services.declaration import DECLARATION_TEXT

    script_path = Path(__file__).resolve().parents[1] / "packaging" / "load_adapter.py"
    script_src = script_path.read_text(encoding="utf-8") if script_path.is_file() else ""
    readme = f"""# Helix Double Helix trained package

Base model: {model.get('id')}
Name: {model.get('name')}
License (base): {model.get('license')}
Method: QLoRA adapter only
Gold rows used: {job.gold_count}

## What is in this zip
- `qlora/` — trained adapter weights (`adapter_model.safetensors` + `adapter_config.json`)
- `tokenizer/` — tokenizer files for serving/merging
- `load_adapter.py` — PEFT loader (run this)
- `data/train_chat.jsonl` — the gold Helix used (from your account)
- `DECLARATION.txt` — ownership and liability text you accepted
- This is **not** a merged full-size model. A 7B–30B fp16 dump is 15–60 GB
  and is not hosted here. Load the adapter on top of the public base.

## Load with the script (recommended)
```bash
pip install torch transformers peft accelerate
python load_adapter.py --prompt "How do I reset my password?"
# optional merge:
python load_adapter.py --merge-to ./merged
```

## Load with PEFT (same steps the script runs)
```python
from transformers import AutoModelForCausalLM, AutoTokenizer
from peft import PeftModel

base = "{model.get('id')}"
tok = AutoTokenizer.from_pretrained("tokenizer")
model = AutoModelForCausalLM.from_pretrained(base, device_map="auto")
model = PeftModel.from_pretrained(model, "qlora")
```

Private storage copies:
- dataset: {job.hf_dataset_repo or "—"}
- adapter: {job.hf_model_repo or "—"}
"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README.md", readme)
        zf.writestr("LICENSE.txt", LICENSE_NOTE)
        zf.writestr("USAGE_AND_LIABILITY.txt", USAGE_OWNERSHIP)
        zf.writestr("DECLARATION.txt", DECLARATION_TEXT)
        if script_src:
            zf.writestr("load_adapter.py", script_src)
        zf.writestr("data/train_chat.jsonl", chat_text)
        zf.writestr(
            "meta.json",
            json.dumps(
                {
                    "job_id": job.id,
                    "base_model": model.get("id"),
                    "base_model_name": model.get("name"),
                    "license": model.get("license"),
                    "params_b": model.get("params_b"),
                    "training": "qlora",
                    "gold_count": job.gold_count,
                    "hf_dataset_repo": job.hf_dataset_repo,
                    "hf_model_repo": job.hf_model_repo,
                    "includes_full_merged_weights": False,
                    "runpod": "serverless",
                },
                indent=2,
            ),
        )
        for p in collect_named_files(adapter_dir, ADAPTER_NAMES):
            zf.write(p, f"qlora/{p.name}")
        tok_root = tokenizer_dir or adapter_dir
        for p in collect_named_files(tok_root, TOKENIZER_NAMES):
            zf.write(p, f"tokenizer/{p.name}")
        if tokenizer_dir and tokenizer_dir != adapter_dir:
            for p in collect_named_files(adapter_dir, TOKENIZER_NAMES):
                arc = f"tokenizer/{p.name}"
                if arc not in zf.namelist():
                    zf.write(p, arc)
    return buf.getvalue()


def _advance_packaging(db: Session, job: m.DoubleHelixTrainJob) -> None:
    token = _hf_token()
    repo = (job.hf_model_repo or "").strip()
    if not repo:
        _fail(job, "Training finished but no adapter repo was recorded.")
        return
    tmp = Path(tempfile.mkdtemp(prefix=f"helix-{job.id}-"))
    try:
        append_train_activity(job, "Downloading trained adapter files…")
        db.commit()
        adapter_dir = tmp / "adapter"
        download_repo_files(token, repo_id=repo, dest=adapter_dir, repo_type="model")
        adapters = collect_named_files(adapter_dir, ADAPTER_NAMES)
        has_weights = any(
            p.name.startswith("adapter_model") for p in adapters
        )
        if not has_weights:
            _fail(
                job,
                "Training reported complete but no adapter weights were pushed. "
                "The worker may have failed to upload. Check the training logs.",
            )
            return
        tok_dir = tmp / "tokenizer"
        tok_dir.mkdir(parents=True, exist_ok=True)
        try:
            download_repo_files(
                token,
                repo_id=job.base_model_id,
                dest=tok_dir,
                repo_type="model",
                allow_patterns=list(TOKENIZER_NAMES),
            )
        except Exception:  # noqa: BLE001
            tok_dir = adapter_dir
        gold_rows = load_trainable_gold(
            db, owner_user_id=job.owner_user_id, tenant_id=job.tenant_id
        )
        blob = build_trained_zip(
            job=job,
            adapter_dir=adapter_dir,
            tokenizer_dir=tok_dir,
            gold_rows=gold_rows,
        )
        rel = f"{job.id}.zip"
        dest = artifacts_dir() / rel
        dest.write_bytes(blob)
        job.artifact_relpath = rel
        job.status = "completed"
        append_train_activity(
            job,
            "Ready. Download the zip of QLoRA adapter, tokenizer, and the gold used.",
        )
        job.finished_at = _now()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def tick_train_job(db: Session, job: m.DoubleHelixTrainJob) -> m.DoubleHelixTrainJob:
    try:
        if job.status == "cancelled":
            return job
        if job.status == "queued":
            _advance_queued(db, job)
        elif job.status == "uploading":
            if job.runpod_job_id:
                job.status = "running"
            else:
                job.status = "queued"
                _advance_queued(db, job)
        elif job.status == "running":
            _advance_running(job)
            if job.status == "packaging":
                db.commit()
                _advance_packaging(db, job)
        elif job.status == "packaging":
            _advance_packaging(db, job)
    except Exception as e:  # noqa: BLE001
        _fail(job, str(e)[:2000])
    job.updated_at = _now()
    db.commit()
    db.refresh(job)
    return job


def pick_train_job(db: Session) -> m.DoubleHelixTrainJob | None:
    for status in ("packaging", "running", "uploading", "queued"):
        row = (
            db.query(m.DoubleHelixTrainJob)
            .filter_by(status=status)
            .order_by(m.DoubleHelixTrainJob.updated_at.asc())
            .first()
        )
        if row:
            return row
    return None


def artifact_file(job: m.DoubleHelixTrainJob) -> Path | None:
    if not job.artifact_relpath:
        return None
    path = artifacts_dir() / job.artifact_relpath
    return path if path.is_file() else None
