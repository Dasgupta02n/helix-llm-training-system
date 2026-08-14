"""Hugging Face Hub helpers for Double Helix train I/O. Token is never logged."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

WHOAMI_URL = "https://huggingface.co/api/whoami-v2"
CREATE_REPO_URL = "https://huggingface.co/api/repos/create"

TOKENIZER_NAMES = (
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "vocab.json",
    "merges.txt",
    "added_tokens.json",
    "tokenizer.model",
    "spiece.model",
    "chat_template.jinja",
    "chat_template.json",
)

ADAPTER_NAMES = (
    "adapter_config.json",
    "adapter_model.safetensors",
    "adapter_model.bin",
    "adapter_model.pt",
    "README.md",
    "training_args.bin",
)


def _auth_headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "HelixDoubleHelix/1.0",
    }


def hf_whoami(token: str) -> dict[str, Any]:
    tok = (token or "").strip()
    if not tok:
        raise ValueError("HF_TOKEN is empty.")
    req = urllib.request.Request(WHOAMI_URL, headers=_auth_headers(tok), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        raise ValueError(f"Hugging Face login failed ({e.code}). Check that HF_TOKEN is valid.") from e
    name = (data.get("name") or data.get("fullname") or "").strip()
    if not name:
        raise ValueError("Hugging Face token has no username (whoami returned empty).")
    data["name"] = name
    return data


def create_private_repo(token: str, *, name: str, repo_type: str) -> str:
    """Create a private dataset or model repo. Returns repo_id (user/name)."""
    me = hf_whoami(token)
    owner = me["name"]
    body = {
        "name": name,
        "private": True,
        "type": repo_type,
        "exist_ok": True,
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        CREATE_REPO_URL,
        data=data,
        method="POST",
        headers={**_auth_headers(token), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            payload = json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")[:500]
        if e.code in {409, 400} and "already" in err.lower():
            return f"{owner}/{name}"
        raise ValueError(
            f"Could not create Hugging Face {repo_type} `{owner}/{name}` ({e.code}). "
            "The token needs write access."
        ) from e
    repo_id = payload.get("name") or payload.get("id") or f"{owner}/{name}"
    if "/" not in str(repo_id):
        repo_id = f"{owner}/{name}"
    return str(repo_id)


def _plural(repo_type: str) -> str:
    return "datasets" if repo_type == "dataset" else "models"


def upload_text_files(
    token: str,
    *,
    repo_id: str,
    repo_type: str,
    files: dict[str, str],
) -> None:
    """Upload small text files via Hub commit API (no huggingface_hub package)."""
    import base64

    if not files:
        return
    lines = [
        json.dumps(
            {
                "key": "header",
                "value": {
                    "summary": "Helix: upload gold / notes",
                    "numFilesToAdd": len(files),
                },
            }
        )
    ]
    for path_in_repo, text in files.items():
        raw = (text or "").encode("utf-8")
        lines.append(
            json.dumps(
                {
                    "key": "file",
                    "value": {
                        "path": path_in_repo,
                        "encoding": "base64",
                        "content": base64.b64encode(raw).decode("ascii"),
                    },
                }
            )
        )
    url = f"https://huggingface.co/api/{_plural(repo_type)}/{repo_id}/commit/main"
    req = urllib.request.Request(
        url,
        data=("\n".join(lines) + "\n").encode("utf-8"),
        method="POST",
        headers={
            **_auth_headers(token),
            "Content-Type": "application/x-ndjson",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            resp.read()
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")[:500]
        raise ValueError(
            f"Hugging Face upload to `{repo_id}` failed ({e.code}). {err}"
        ) from e


def _list_repo_paths(token: str, repo_id: str, repo_type: str) -> list[str]:
    url = (
        f"https://huggingface.co/api/{_plural(repo_type)}/{repo_id}/tree/main"
        "?recursive=1"
    )
    req = urllib.request.Request(url, headers=_auth_headers(token), method="GET")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")[:300]
        raise ValueError(f"Could not list `{repo_id}` ({e.code}). {err}") from e
    if not isinstance(data, list):
        return []
    out: list[str] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        if item.get("type") in {"file", None} and item.get("path"):
            out.append(str(item["path"]))
    return out


def download_repo_files(
    token: str,
    *,
    repo_id: str,
    dest: Path,
    repo_type: str = "model",
    allow_patterns: list[str] | None = None,
) -> Path:
    dest.mkdir(parents=True, exist_ok=True)
    wanted = {p.lower() for p in (allow_patterns or [])}
    paths = _list_repo_paths(token, repo_id, repo_type)
    if wanted:
        paths = [p for p in paths if Path(p).name.lower() in wanted]
    if not paths:
        raise ValueError(f"No matching files in Hugging Face repo `{repo_id}`.")
    prefix = "datasets/" if repo_type == "dataset" else ""
    for rel in paths:
        url = f"https://huggingface.co/{prefix}{repo_id}/resolve/main/{rel}"
        req = urllib.request.Request(url, headers=_auth_headers(token), method="GET")
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                target.write_bytes(resp.read())
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")[:200]
            raise ValueError(f"Download `{rel}` from `{repo_id}` failed ({e.code}). {err}") from e
    return dest


def collect_named_files(root: Path, names: tuple[str, ...]) -> list[Path]:
    found: list[Path] = []
    if not root.exists():
        return found
    wanted = {n.lower() for n in names}
    for p in root.rglob("*"):
        if p.is_file() and p.name.lower() in wanted:
            found.append(p)
    return found
