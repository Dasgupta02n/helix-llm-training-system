"""C7X-IO v1: package gold as QLoRA-ready files for a chosen Apache/MIT model.

Does not start a GPU job unless RUNPOD_API_KEY is configured.
"""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any

from helix.services.base_models import get_model, public_models, recommend_model
from helix.services.library import gold_to_chat_messages
from helix.services.role_risk import RECOMMENDED_TRAINING

LICENSE_NOTE = """This package is built for a base model licensed Apache-2.0 or MIT.

C7X does not redistribute base weights. Download the model from its official
card under that model's license. You are responsible for complying with it.

Llama / Gemma / other non-Apache-non-MIT bases are not offered in C7X-IO.
"""

USAGE_OWNERSHIP = """USAGE, OWNERSHIP, AND LIABILITY

1. You own the gold JSONL in this zip. C7X does not claim copyright in your examples.
2. The fine-tuned adapter (if you train it) is yours to run locally.
3. C7X is not liable for decisions made by a model you train — especially hiring,
   credit, medical, or other high-risk roles. Human review remains required.
4. Training method for v1 is QLoRA only on the Apache-2.0 / MIT model you selected (≤30B).
5. No payment is collected. Access is limited to admin-approved C7X accounts.
"""


def _readme(model: dict[str, Any]) -> str:
    return f"""# C7X-IO v1 package

Base model: {model['id']}
Name: {model['name']}
License: {model['license']}
Size: {model['params_b']}B
Method: {RECOMMENDED_TRAINING} only
QLoRA VRAM hint: ~{model['vram_gb_qlora']} GB

## Files
- data/train_chat.jsonl — chat-format gold (user/assistant)
- LICENSE.txt — Apache-2.0 / MIT reminder (see the model card for the exact text)
- USAGE_AND_LIABILITY.txt

## Ollama (local)
1. Pull the base from its official Apache/MIT model card.
2. After you train a QLoRA adapter, convert/export per your trainer.
3. `ollama create C7X-custom -f Modelfile` then `ollama run C7X-custom`

## vLLM
Serve the merged or adapter-backed model you trained.

## Pay-per-run GPU
C7X/Riu always trains on **pay-per-run GPU** (idle when unused).
Always-on machines are never used — they stay billed if left running.
"""


def build_package_zip(
    rows: list[dict[str, Any]],
    *,
    model_id: str | None = None,
) -> bytes:
    model = get_model(model_id or "") or recommend_model()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        lines = []
        for row in rows:
            chat = gold_to_chat_messages(
                row.get("input") or "",
                row.get("output") or "",
            )
            lines.append(json.dumps(chat, ensure_ascii=False))
        zf.writestr("data/train_chat.jsonl", "\n".join(lines) + ("\n" if lines else ""))
        zf.writestr("LICENSE.txt", LICENSE_NOTE)
        zf.writestr("USAGE_AND_LIABILITY.txt", USAGE_OWNERSHIP)
        zf.writestr("README.md", _readme(model))
        zf.writestr(
            "meta.json",
            json.dumps(
                {
                    "base_model": model["id"],
                    "base_model_name": model["name"],
                    "license": model["license"],
                    "params_b": model["params_b"],
                    "training": RECOMMENDED_TRAINING,
                    "rows": len(rows),
                    "catalog": [
                        {"id": m["id"], "name": m["name"], "license": m["license"], "params_b": m["params_b"]}
                        for m in public_models()
                    ],
                    "runpod": "serverless",
                    "runpod_pods": False,
                },
                indent=2,
            ),
        )
    return buf.getvalue()
