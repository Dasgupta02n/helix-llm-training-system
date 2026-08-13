"""Double Helix v1: package gold as QLoRA-ready files for Llama 3.1 8B.

Does not start a GPU job unless RUNPOD_API_KEY is configured. Always returns
a zip the account owner can run locally (Ollama / vLLM notes included).
"""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any

from helix.services.library import gold_to_chat_messages
from helix.services.role_risk import RECOMMENDED_BASE_MODEL, RECOMMENDED_TRAINING

LLAMA_LICENSE_NOTE = """Llama 3.1 Community License (Meta).
You must accept Meta's license before downloading or fine-tuning Llama 3.1 8B weights:
https://www.llama.com/llama3_1/license/

Helix does not redistribute Meta weights. This zip contains YOUR gold data plus
instructions. You are responsible for obtaining the base model legally.
"""

USAGE_OWNERSHIP = """USAGE, OWNERSHIP, AND LIABILITY

1. You own the gold JSONL in this zip. Helix does not claim copyright in your examples.
2. The fine-tuned adapter (if you train it) is yours to run locally.
3. Helix is not liable for decisions made by a model you train — especially hiring,
   credit, medical, or other high-risk roles. Human review remains required.
4. Training method for v1 is QLoRA only on {model}. No full-rank LoRA, no other bases.
5. No payment is collected. Access is limited to admin-approved Helix accounts.
""".format(model=RECOMMENDED_BASE_MODEL)

README = """# Double Helix v1 package

Base model: {model}
Method: {method} only

## Files
- data/train_chat.jsonl — chat-format gold (user/assistant)
- LICENSE_LLAMA.txt
- USAGE_AND_LIABILITY.txt

## Ollama (local)
1. Obtain Llama 3.1 8B under Meta's license.
2. After you train a QLoRA adapter, convert/export per your trainer.
3. `ollama create helix-custom -f Modelfile` then `ollama run helix-custom`

## vLLM
Serve the merged or adapter-backed model you trained. Helix does not host GPUs
unless you separately configure RunPod.

## RunPod
If RUNPOD_API_KEY is set on the Helix server, operators can launch a QLoRA job.
Otherwise use this zip on your own GPU.
""".format(model=RECOMMENDED_BASE_MODEL, method=RECOMMENDED_TRAINING)


def build_package_zip(rows: list[dict[str, Any]]) -> bytes:
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
        zf.writestr("LICENSE_LLAMA.txt", LLAMA_LICENSE_NOTE)
        zf.writestr("USAGE_AND_LIABILITY.txt", USAGE_OWNERSHIP)
        zf.writestr("README.md", README)
        zf.writestr(
            "meta.json",
            json.dumps(
                {
                    "base_model": RECOMMENDED_BASE_MODEL,
                    "training": RECOMMENDED_TRAINING,
                    "rows": len(rows),
                    "runpod": False,
                },
                indent=2,
            ),
        )
    return buf.getvalue()
