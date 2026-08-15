"""Apache-2.0 / MIT instruct models ≤30B for C7X-IO.

Llama is intentionally absent (Meta Community License is not Apache/MIT).
Short curated list — not every open checkpoint on the Hub.
"""

from __future__ import annotations

import re
from typing import Any

# Fallback GPU dollars/second used when a job does not return a billed amount.
# Matches helix.config.compute_usd_per_second. Usage shown to the user is 2× this.
_GPU_USD_PER_SEC = 0.00076
_USAGE_MARKUP = 2.0

# Qwen2.5-7B-Instruct: Apache-2.0 on the model card. Strong default for one role.
DEFAULT_MODEL_ID = "Qwen/Qwen2.5-7B-Instruct"
DEFAULT_REASON = (
    "Recommended for a role-specific assistant: Apache-2.0, 7B, strong instruction "
    "following. Small enough that a typical 1-epoch QLoRA stays in the "
    "pay-per-job band below; larger than a 1.7B smoke-test model."
)

# QLoRA vRAM is a planning hint, not a hard scheduler.
# Licenses checked 2026-08-16 against the official model cards:
#   Qwen2.5-7B/14B Instruct → Apache-2.0 (3B and 72B are not)
#   Mistral-7B-Instruct-v0.3 → Apache-2.0
#   SmolLM2-1.7B-Instruct → Apache-2.0
MODELS: list[dict[str, Any]] = [
    {
        "id": DEFAULT_MODEL_ID,
        "name": "Qwen2.5 7B Instruct",
        "params_b": 7.6,
        "license": "Apache-2.0",
        "family": "Qwen",
        "vram_gb_qlora": 16,
        "best_for": "Default for a role-specific assistant — stronger general quality.",
        "train_minutes": (90, 300),
        "recommended": True,
    },
    {
        "id": "mistralai/Mistral-7B-Instruct-v0.3",
        "name": "Mistral 7B Instruct v0.3",
        "params_b": 7.3,
        "license": "Apache-2.0",
        "family": "Mistral",
        "vram_gb_qlora": 16,
        "best_for": "Same size class as the default; strong general 7B alternative.",
        "train_minutes": (90, 300),
        "recommended": False,
    },
    {
        "id": "Qwen/Qwen2.5-14B-Instruct",
        "name": "Qwen2.5 14B Instruct",
        "params_b": 14.7,
        "license": "Apache-2.0",
        "family": "Qwen",
        "vram_gb_qlora": 24,
        "best_for": "Stronger quality when you accept a longer, costlier GPU job.",
        "train_minutes": (150, 420),
        "recommended": False,
    },
    {
        "id": "HuggingFaceTB/SmolLM2-1.7B-Instruct",
        "name": "SmolLM2 1.7B Instruct",
        "params_b": 1.7,
        "license": "Apache-2.0",
        "family": "SmolLM",
        "vram_gb_qlora": 8,
        "best_for": "Fast and cheap testing — too small to replace a real role.",
        "train_minutes": (20, 120),
        "recommended": False,
    },
]

MAX_PARAMS_B = 30.0
ALLOWED_LICENSES = frozenset({"Apache-2.0", "MIT"})


def _usd_band(minutes: tuple[int, int]) -> tuple[int, int]:
    """User-facing usage = 2 × GPU dollars at the configured fallback rate.

    Minutes include a conservative span (short gold → large gold, plus
    queue/upload). Not an invoice. A billed job replaces this when it returns.
    """
    lo, hi = minutes
    # 1.4× covers queue + Hub upload/download around the train loop.
    pad = 1.4
    raw_lo = lo * 60 * _GPU_USD_PER_SEC * _USAGE_MARKUP * pad
    raw_hi = hi * 60 * _GPU_USD_PER_SEC * _USAGE_MARKUP * pad
    return max(5, int(round(raw_lo / 5.0) * 5)), max(10, int(round(raw_hi / 5.0) * 5))


def _enrich(m: dict[str, Any]) -> dict[str, Any]:
    lo, hi = _usd_band(tuple(m.get("train_minutes") or (45, 240)))
    out = dict(m)
    out["train_usd_min"] = lo
    out["train_usd_max"] = hi
    out["recommended"] = bool(m.get("recommended")) or m["id"] == DEFAULT_MODEL_ID
    out["best_for"] = m.get("best_for") or ""
    return out


def public_models() -> list[dict[str, Any]]:
    rows = [
        _enrich(m)
        for m in MODELS
        if not m.get("hidden")
        and float(m["params_b"]) <= MAX_PARAMS_B
        and m["license"] in ALLOWED_LICENSES
    ]
    rows.sort(key=lambda r: (0 if r["recommended"] else 1, r["params_b"]))
    return rows


def default_model() -> dict[str, Any]:
    return get_model(DEFAULT_MODEL_ID) or public_models()[0]


def catalog_payload() -> dict[str, Any]:
    models = public_models()
    rec = default_model()
    return {
        "max_params_b": MAX_PARAMS_B,
        "licenses": sorted(ALLOWED_LICENSES),
        "excluded": ["Llama (Meta Community License)", "Gemma (Gemma license)"],
        "default_id": rec["id"],
        "default_reason": DEFAULT_REASON,
        "models": models,
    }


def get_model(model_id: str) -> dict[str, Any] | None:
    mid = (model_id or "").strip()
    for m in public_models():
        if m["id"] == mid or m["name"].lower() == mid.lower():
            return m
    return None


def recommend_model(*, role_type: str = "", risk_level: str = "medium") -> dict[str, Any]:
    """One default for a role-specific assistant. Catalog still offers 1.7B and 14B."""
    del role_type, risk_level
    return default_model()


def format_model_menu() -> str:
    rec = default_model()
    lines = [
        "Apache-2.0 / MIT instruct models **up to 30B** "
        "(Llama is not listed — Meta’s license is not Apache/MIT):",
        f"**Recommended default:** {rec['name']} — {DEFAULT_REASON}",
    ]
    for m in public_models():
        tag = " **recommended**" if m.get("recommended") else ""
        lines.append(
            f"- **{m['name']}**{tag} (`{m['id']}`) · {m['params_b']}B · {m['license']} · "
            f"{m.get('best_for') or ''} · typical GPU job ~${m['train_usd_min']}–${m['train_usd_max']}"
        )
    lines.append("Reply with the model name, or keep the recommended default.")
    return "\n".join(lines)


def resolve_user_model_choice(text: str) -> dict[str, Any] | None:
    blob = (text or "").strip().lower()
    if not blob:
        return None
    for m in public_models():
        if m["id"].lower() in blob or m["name"].lower() in blob:
            return m
    for m in public_models():
        fam = m["family"].lower()
        model_nums = set(re.findall(r"\d+", f"{m['name']} {m['id']}"))
        user_nums = set(re.findall(r"\d+", blob))
        if fam in blob and model_nums & user_nums:
            return m
    return None
