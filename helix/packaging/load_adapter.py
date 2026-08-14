#!/usr/bin/env python3
"""Load a Helix Double Helix QLoRA adapter with PEFT.

Unzip the trained package and run this file from the unzipped folder:

    pip install torch transformers peft accelerate
    python load_adapter.py --prompt "How do I reset my password?"

Optional:
    python load_adapter.py --merge-to ./merged
    python load_adapter.py --base Qwen/Qwen2.5-7B-Instruct --device cpu
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _read_meta() -> dict:
    meta_path = ROOT / "meta.json"
    if not meta_path.is_file():
        return {}
    return json.loads(meta_path.read_text(encoding="utf-8"))


def load_peft_model(base_id: str, device: str = "auto"):
    try:
        from peft import PeftModel
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError as e:
        raise SystemExit(
            "Missing packages. Install with:\n"
            "  pip install torch transformers peft accelerate"
        ) from e

    tok_dir = ROOT / "tokenizer"
    adapter_dir = ROOT / "qlora"
    if not adapter_dir.is_dir():
        raise SystemExit(f"No qlora/ folder next to this script ({adapter_dir})")

    tok_src = str(tok_dir) if tok_dir.is_dir() else base_id
    tokenizer = AutoTokenizer.from_pretrained(tok_src, trust_remote_code=True)
    model = AutoModelForCausalLM.from_pretrained(
        base_id,
        device_map=device,
        torch_dtype="auto",
        trust_remote_code=True,
    )
    model = PeftModel.from_pretrained(model, str(adapter_dir))
    model.eval()
    return tokenizer, model


def generate(tokenizer, model, prompt: str, max_new_tokens: int = 256) -> str:
    import torch

    messages = [{"role": "user", "content": prompt}]
    if getattr(tokenizer, "apply_chat_template", None):
        text = tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True
        )
    else:
        text = prompt
    inputs = tokenizer(text, return_tensors="pt")
    device = next(model.parameters()).device
    inputs = {k: v.to(device) for k, v in inputs.items()}
    with torch.no_grad():
        out = model.generate(
            **inputs,
            max_new_tokens=max_new_tokens,
            do_sample=False,
        )
    return tokenizer.decode(out[0], skip_special_tokens=True)


def main(argv: list[str] | None = None) -> int:
    meta = _read_meta()
    default_base = str(meta.get("base_model") or "").strip()
    parser = argparse.ArgumentParser(
        description="Load the Helix QLoRA adapter in this folder with PEFT."
    )
    parser.add_argument(
        "--base",
        default=default_base,
        help="Hugging Face id of the Apache/MIT base model",
    )
    parser.add_argument("--prompt", default="", help="Optional one-shot generation")
    parser.add_argument("--max-new-tokens", type=int, default=256)
    parser.add_argument(
        "--device",
        default="auto",
        help="device_map for transformers (auto, cpu, cuda)",
    )
    parser.add_argument(
        "--merge-to",
        default="",
        help="If set, merge adapter into the base and save to this folder",
    )
    args = parser.parse_args(argv)
    if not args.base:
        print("Pass --base MODEL_ID (or include meta.json with base_model).", file=sys.stderr)
        return 2

    tokenizer, model = load_peft_model(args.base, device=args.device)
    print(f"Loaded PEFT adapter from {ROOT / 'qlora'} on base {args.base}")

    if args.merge_to:
        merged = model.merge_and_unload()
        dest = Path(args.merge_to)
        dest.mkdir(parents=True, exist_ok=True)
        merged.save_pretrained(dest)
        tokenizer.save_pretrained(dest)
        print(f"Merged weights saved to {dest}")

    if args.prompt:
        print(generate(tokenizer, model, args.prompt, args.max_new_tokens))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
