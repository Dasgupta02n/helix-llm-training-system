"""Double Helix GPU backend: RunPod Serverless only.

Helix never creates or bills a GPU Cloud pod. Training is pay-per-run on a
Serverless endpoint with min workers = 0 (idle = $0).
"""

from __future__ import annotations

from typing import Any

COMPUTE_BACKEND = "runpod_serverless"
# Product lock — Riu must not offer GPU Cloud / "always-on" pods.
FORBIDDEN_BACKENDS = frozenset({"runpod_pod", "pod", "gpu_cloud"})

RUNPOD_SERVERLESS_RUN = "https://api.runpod.ai/v2/{endpoint_id}/run"
RUNPOD_SERVERLESS_STATUS = "https://api.runpod.ai/v2/{endpoint_id}/status/{job_id}"


def assert_serverless_only(backend: str | None = None) -> None:
    b = (backend or COMPUTE_BACKEND).strip().lower()
    if b in FORBIDDEN_BACKENDS or b != COMPUTE_BACKEND:
        raise ValueError(
            "Double Helix trains only on pay-per-run GPU. "
            "Always-on machines are not used (they can sit on and bill)."
        )


def _settings_tuple() -> tuple[str, str, str]:
    from helix.config import get_settings

    s = get_settings()
    key = (getattr(s, "runpod_api_key", "") or "").strip()
    endpoint = (getattr(s, "runpod_serverless_endpoint_id", "") or "").strip()
    hf = (getattr(s, "hf_token", "") or "").strip()
    return key, endpoint, hf


def runpod_configured() -> bool:
    key, endpoint, _hf = _settings_tuple()
    return bool(key and endpoint)


def hf_token_configured() -> bool:
    return bool(_settings_tuple()[2])


def train_ready() -> bool:
    key, endpoint, hf = _settings_tuple()
    return bool(key and endpoint and hf)


def compute_policy() -> dict[str, Any]:
    ready = train_ready()
    return {
        "backend": "pay_per_run",
        "label": "Pay-per-run GPU (pay per train job)",
        "min_workers": 0,
        "idle_charge": False,
        "forbidden": sorted(FORBIDDEN_BACKENDS),
        "gpu_configured": runpod_configured(),
        "storage_token_set": hf_token_configured(),
        "train_ready": ready,
        "estimated_usd_min": 15,
        "estimated_usd_max": 50,
        "note": (
            "Helix always uses pay-per-run GPU. Do not leave an always-on "
            "machine running — idle machines bill until you stop them. "
            "A job that idles at zero workers costs nothing until "
            "Riu submits a QLoRA train."
        ),
    }


def official_qlora_input(
    *,
    run_id: str,
    base_model: str,
    dataset_repo: str,
    hub_model_id: str,
    hf_token: str,
    gold_count: int = 0,
) -> dict[str, Any]:
    """Payload the official runpod/llm-finetuning worker expects."""
    val = 0.05 if int(gold_count or 0) >= 40 else 0.0
    return {
        "run_id": run_id,
        "credentials": {
            "wandb_api_key": "",
            "hf_token": hf_token,
        },
        "args": {
            "base_model": base_model,
            "load_in_4bit": True,
            "strict": False,
            "datasets": [
                {
                    "path": dataset_repo,
                    "type": "alpaca",
                    "ds_type": "json",
                    "data_files": "train_alpaca.jsonl",
                }
            ],
            "dataset_prepared_path": "last_run_prepared",
            "val_set_size": val,
            "output_dir": "./outputs/qlora-out",
            "adapter": "qlora",
            "sequence_len": 2048,
            "sample_packing": True,
            "eval_sample_packing": False,
            "pad_to_sequence_len": True,
            "lora_r": 16,
            "lora_alpha": 32,
            "lora_dropout": 0.05,
            "lora_target_linear": True,
            "gradient_accumulation_steps": 4,
            "micro_batch_size": 1,
            "num_epochs": 1,
            "optimizer": "adamw_8bit",
            "lr_scheduler": "cosine",
            "learning_rate": 0.0002,
            "train_on_inputs": False,
            "group_by_length": False,
            "bf16": "auto",
            "tf32": False,
            "gradient_checkpointing": True,
            "logging_steps": 10,
            "flash_attention": True,
            "warmup_steps": 10,
            "saves_per_epoch": 1,
            "weight_decay": 0,
            "hub_model_id": hub_model_id,
            "hub_strategy": "end",
        },
    }


def submit_qlora_job(
    *,
    model_id: str,
    dataset_uri: str | None = None,
    dataset_jsonl: str | None = None,
    hf_token: str | None = None,
    run_id: str | None = None,
    dataset_repo: str | None = None,
    hub_model_id: str | None = None,
    gold_count: int = 0,
) -> dict[str, Any]:
    """Queue a QLoRA train on the Serverless endpoint. Never opens a pod."""
    assert_serverless_only()
    key, endpoint, default_hf = _settings_tuple()
    token = (hf_token or default_hf or "").strip()
    if not key:
        return {
            "ok": False,
            "backend": COMPUTE_BACKEND,
            "error": "Training credentials are not set on the server.",
        }
    if not endpoint:
        return {
            "ok": False,
            "backend": COMPUTE_BACKEND,
            "error": (
                "The training endpoint is not set. Configure pay-per-run GPU "
                "(idle when unused) on the server."
            ),
        }
    if not token:
        return {
            "ok": False,
            "backend": COMPUTE_BACKEND,
            "error": "Model-storage credentials are not set on the server.",
        }
    repo = (dataset_repo or dataset_uri or "").strip()
    if not repo:
        return {
            "ok": False,
            "backend": COMPUTE_BACKEND,
            "error": "dataset_repo is required (exported gold from this account).",
        }

    import json
    import uuid
    import urllib.request

    rid = (run_id or f"helix-{uuid.uuid4().hex[:12]}").strip()
    hub = (hub_model_id or "").strip() or f"helix-qlora-{rid}"
    payload = official_qlora_input(
        run_id=rid,
        base_model=model_id,
        dataset_repo=repo,
        hub_model_id=hub,
        hf_token=token,
        gold_count=gold_count,
    )
    # dataset_jsonl is unused: official worker cannot take inline gold.
    _ = dataset_jsonl

    url = RUNPOD_SERVERLESS_RUN.format(endpoint_id=endpoint)
    body = {"input": payload}
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "backend": COMPUTE_BACKEND, "error": str(e)[:500]}
    return {
        "ok": True,
        "backend": COMPUTE_BACKEND,
        "endpoint_id": endpoint,
        "run_id": rid,
        "hub_model_id": hub,
        "runpod": raw,
        "runpod_job_id": (raw or {}).get("id") if isinstance(raw, dict) else None,
    }


def poll_qlora_job(runpod_job_id: str) -> dict[str, Any]:
    """Read Serverless job status. Never opens a pod."""
    import json
    import urllib.request

    key, endpoint, _hf = _settings_tuple()
    if not key or not endpoint or not (runpod_job_id or "").strip():
        return {"ok": False, "error": "Training is not configured or job id is missing."}
    url = RUNPOD_SERVERLESS_STATUS.format(
        endpoint_id=endpoint, job_id=runpod_job_id.strip()
    )
    req = urllib.request.Request(
        url,
        method="GET",
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)[:500]}
    status = ""
    if isinstance(raw, dict):
        status = str(raw.get("status") or raw.get("state") or "").upper()
    return {
        "ok": True,
        "status": status,
        "runpod": raw,
        "compute_cost_usd": extract_compute_cost_usd(raw),
    }


def extract_compute_cost_usd(raw: Any) -> float:
    """Pull billed compute $ from a job payload, or estimate from execution time."""
    if not isinstance(raw, dict):
        return 0.0
    for key in ("cost", "costUsd", "cost_usd", "totalCost", "total_cost"):
        val = raw.get(key)
        if val is None and isinstance(raw.get("output"), dict):
            val = raw["output"].get(key)
        try:
            f = float(val)
        except (TypeError, ValueError):
            continue
        if f >= 0:
            return round(f, 6)
    ms = raw.get("executionTime") or raw.get("executionTimeMs") or raw.get("execution_time")
    try:
        t = float(ms)
    except (TypeError, ValueError):
        return 0.0
    # Values > 120 are almost always milliseconds.
    seconds = t / 1000.0 if t > 120 else t
    from helix.config import get_settings

    rate = float(getattr(get_settings(), "compute_usd_per_second", 0.0) or 0.00076)
    return round(max(0.0, seconds) * rate, 6)
