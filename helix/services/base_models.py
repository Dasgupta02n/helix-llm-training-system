"""Apache-2.0 / MIT instruct models ≤30B for Double Helix.

Llama is intentionally absent (Meta Community License is not Apache/MIT).
This is first-party instruct checkpoints only — not every HF fine-tune.
"""

from __future__ import annotations

import re
from typing import Any

# QLoRA vRAM is a planning hint, not a hard scheduler.
MODELS: list[dict[str, Any]] = [
    {
        "id": "HuggingFaceTB/SmolLM2-1.7B-Instruct",
        "name": "SmolLM2 1.7B Instruct",
        "params_b": 1.7,
        "license": "Apache-2.0",
        "family": "SmolLM",
        "vram_gb_qlora": 8,
        "good_for": ["captions", "copy", "tiny on-device"],
    },
    {
        "id": "microsoft/Phi-3.5-mini-instruct",
        "name": "Phi-3.5 Mini Instruct",
        "params_b": 3.8,
        "license": "MIT",
        "family": "Phi",
        "vram_gb_qlora": 10,
        "good_for": ["captions", "faq", "fast iteration"],
    },
    {
        "id": "Qwen/Qwen2.5-3B-Instruct",
        "name": "Qwen2.5 3B Instruct",
        "params_b": 3.0,
        "license": "Apache-2.0",
        "family": "Qwen",
        "vram_gb_qlora": 10,
        "good_for": ["faq", "copy", "multilingual small"],
    },
    {
        "id": "ibm-granite/granite-3.1-2b-instruct",
        "name": "Granite 3.1 2B Instruct",
        "params_b": 2.0,
        "license": "Apache-2.0",
        "family": "Granite",
        "vram_gb_qlora": 8,
        "good_for": ["copy", "enterprise small"],
    },
    {
        "id": "ibm-granite/granite-3.1-8b-instruct",
        "name": "Granite 3.1 8B Instruct",
        "params_b": 8.0,
        "license": "Apache-2.0",
        "family": "Granite",
        "vram_gb_qlora": 16,
        "good_for": ["support", "hr", "enterprise"],
    },
    {
        "id": "mistralai/Mistral-7B-Instruct-v0.3",
        "name": "Mistral 7B Instruct v0.3",
        "params_b": 7.3,
        "license": "Apache-2.0",
        "family": "Mistral",
        "vram_gb_qlora": 16,
        "good_for": ["support", "sales", "general"],
    },
    {
        "id": "mistralai/Ministral-8B-Instruct-2410",
        "name": "Ministral 8B Instruct",
        "params_b": 8.0,
        "license": "Apache-2.0",
        "family": "Mistral",
        "vram_gb_qlora": 16,
        "good_for": ["support", "sales", "general"],
    },
    {
        "id": "Qwen/Qwen2.5-7B-Instruct",
        "name": "Qwen2.5 7B Instruct",
        "params_b": 7.6,
        "license": "Apache-2.0",
        "family": "Qwen",
        "vram_gb_qlora": 16,
        "good_for": ["support", "sales", "multilingual"],
    },
    {
        "id": "Qwen/Qwen3-8B",
        "name": "Qwen3 8B",
        "params_b": 8.0,
        "license": "Apache-2.0",
        "family": "Qwen",
        "vram_gb_qlora": 16,
        "good_for": ["support", "sales", "reasoning"],
    },
    {
        "id": "allenai/OLMo-2-1124-7B-Instruct",
        "name": "OLMo 2 7B Instruct",
        "params_b": 7.0,
        "license": "Apache-2.0",
        "family": "OLMo",
        "vram_gb_qlora": 16,
        "good_for": ["research", "fully open stack"],
    },
    {
        "id": "allenai/OLMo-2-1124-13B-Instruct",
        "name": "OLMo 2 13B Instruct",
        "params_b": 13.0,
        "license": "Apache-2.0",
        "family": "OLMo",
        "vram_gb_qlora": 24,
        "good_for": ["research", "fully open stack"],
    },
    {
        "id": "microsoft/Phi-3-medium-4k-instruct",
        "name": "Phi-3 Medium 14B Instruct",
        "params_b": 14.0,
        "license": "MIT",
        "family": "Phi",
        "vram_gb_qlora": 24,
        "good_for": ["support", "education", "reasoning"],
    },
    {
        "id": "microsoft/phi-4",
        "name": "Phi-4 14B",
        "params_b": 14.7,
        "license": "MIT",
        "family": "Phi",
        "vram_gb_qlora": 24,
        "good_for": ["education", "reasoning", "general"],
    },
    {
        "id": "Qwen/Qwen2.5-14B-Instruct",
        "name": "Qwen2.5 14B Instruct",
        "params_b": 14.7,
        "license": "Apache-2.0",
        "family": "Qwen",
        "vram_gb_qlora": 24,
        "good_for": ["support", "sales", "multilingual"],
    },
    {
        "id": "Qwen/Qwen3-14B",
        "name": "Qwen3 14B",
        "params_b": 14.8,
        "license": "Apache-2.0",
        "family": "Qwen",
        "vram_gb_qlora": 24,
        "good_for": ["support", "hiring-assist", "reasoning"],
    },
    {
        "id": "Qwen/Qwen2.5-32B-Instruct",
        "name": "Qwen2.5 32B Instruct",
        "params_b": 32.5,
        "license": "Apache-2.0",
        "family": "Qwen",
        "vram_gb_qlora": 48,
        "good_for": [],  # over 30B — excluded from public list
        "hidden": True,
    },
    {
        "id": "Qwen/Qwen3-30B-A3B",
        "name": "Qwen3 30B-A3B (MoE, 3B active)",
        "params_b": 30.0,
        "license": "Apache-2.0",
        "family": "Qwen",
        "vram_gb_qlora": 40,
        "good_for": ["general", "reasoning", "multilingual"],
        "notes": "Mixture-of-experts: 30B total, ~3B active.",
    },
]

MAX_PARAMS_B = 30.0
ALLOWED_LICENSES = frozenset({"Apache-2.0", "MIT"})


def public_models() -> list[dict[str, Any]]:
    return [
        m
        for m in MODELS
        if not m.get("hidden")
        and float(m["params_b"]) <= MAX_PARAMS_B
        and m["license"] in ALLOWED_LICENSES
    ]


def get_model(model_id: str) -> dict[str, Any] | None:
    mid = (model_id or "").strip()
    for m in public_models():
        if m["id"] == mid or m["name"].lower() == mid.lower():
            return m
    return None


def recommend_model(*, role_type: str = "", risk_level: str = "medium") -> dict[str, Any]:
    """Pick a default; user can still choose any catalog model."""
    catalog = public_models()
    prefer_ids = {
        "high": "Qwen/Qwen3-14B",
        "medium": "Qwen/Qwen2.5-7B-Instruct",
        "low": "microsoft/Phi-3.5-mini-instruct",
    }
    if role_type in {"caption", "copy"}:
        want = "microsoft/Phi-3.5-mini-instruct"
    elif role_type in {"hiring", "credit", "medical", "legal"}:
        want = "Qwen/Qwen3-14B"
    else:
        want = prefer_ids.get(risk_level, "Qwen/Qwen2.5-7B-Instruct")
    return get_model(want) or catalog[0]


def format_model_menu() -> str:
    lines = [
        "Apache-2.0 / MIT instruct models **up to 30B** (Llama is not listed — Meta’s license is not Apache/MIT):"
    ]
    for m in public_models():
        lines.append(
            f"- **{m['name']}** (`{m['id']}`) · {m['params_b']}B · {m['license']} · "
            f"~{m['vram_gb_qlora']} GB VRAM for QLoRA"
        )
    lines.append("Reply with the model name (or keep the recommended default).")
    return "\n".join(lines)


def resolve_user_model_choice(text: str) -> dict[str, Any] | None:
    blob = (text or "").strip().lower()
    if not blob:
        return None
    if "phi-4" in blob or "phi4" in blob:
        hit = get_model("microsoft/phi-4")
        if hit:
            return hit
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
