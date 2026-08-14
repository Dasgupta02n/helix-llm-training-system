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


def compute_policy() -> dict[str, Any]:
    return {
        "backend": COMPUTE_BACKEND,
        "label": "RunPod Serverless (pay per train job)",
        "min_workers": 0,
        "idle_charge": False,
        "forbidden": sorted(FORBIDDEN_BACKENDS),
        "note": (
            "Helix always selects Serverless. Do not create a GPU Cloud pod "
            "in the RunPod console — idle pods bill until you stop them. "
            "A Serverless endpoint with min workers = 0 costs nothing until "
            "Riu submits a QLoRA job."
        ),
    }


def assert_serverless_only(backend: str | None = None) -> None:
    b = (backend or COMPUTE_BACKEND).strip().lower()
    if b in FORBIDDEN_BACKENDS or b != COMPUTE_BACKEND:
        raise ValueError(
            "Double Helix trains only on RunPod Serverless. "
            "GPU Cloud pods are not used (they can sit on and bill)."
        )


def runpod_configured() -> bool:
    from helix.config import get_settings

    s = get_settings()
    return bool((getattr(s, "runpod_api_key", "") or "").strip())


def submit_qlora_job(
    *,
    model_id: str,
    dataset_uri: str | None = None,
    dataset_jsonl: str | None = None,
    hf_token: str | None = None,
) -> dict[str, Any]:
    """Queue a QLoRA train on the Serverless endpoint. Never opens a pod."""
    assert_serverless_only()
    from helix.config import get_settings

    s = get_settings()
    key = (getattr(s, "runpod_api_key", "") or "").strip()
    endpoint = (getattr(s, "runpod_serverless_endpoint_id", "") or "").strip()
    if not key:
        return {
            "ok": False,
            "backend": COMPUTE_BACKEND,
            "error": "RUNPOD_API_KEY is not set on the server.",
        }
    if not endpoint:
        return {
            "ok": False,
            "backend": COMPUTE_BACKEND,
            "error": (
                "RUNPOD_SERVERLESS_ENDPOINT_ID is not set. Create one Serverless "
                "fine-tune endpoint with min workers = 0 (do not use a Pod) and "
                "put its ID in Hostinger env."
            ),
        }

    import json
    import urllib.request

    url = RUNPOD_SERVERLESS_RUN.format(endpoint_id=endpoint)
    body = {
        "input": {
            "base_model": model_id,
            "adapter": "qlora",
            "dataset_uri": dataset_uri,
            "dataset_jsonl": dataset_jsonl,
            "hf_token": hf_token or getattr(s, "hf_token", "") or "",
        }
    }
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
        "runpod": raw,
    }
