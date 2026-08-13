"""Bring-your-own data: zip → gold-format rows for Helix + Double Helix."""

from __future__ import annotations

import csv
import io
import json
import re
import uuid
import zipfile
from typing import Any, BinaryIO

from sqlalchemy.orm import Session

from helix.db import models as m
from helix.services.library import add_gold_example, gold_to_dict

# source_kind for user-supplied labeled data (Double Helix ready)
USER_UPLOAD_SOURCE_KIND = "user_upload"

MAX_ZIP_BYTES = 25 * 1024 * 1024  # 25 MiB
MAX_ROWS_PER_UPLOAD = 5000
MAX_FILES_SCANNED = 200

_INPUT_KEYS = (
    "input",
    "input_text",
    "question",
    "prompt",
    "user",
    "instruction",
    "query",
    "messages",
)
_OUTPUT_KEYS = (
    "output",
    "output_text",
    "answer",
    "completion",
    "response",
    "assistant",
    "ideal",
    "label",
)
_TOPIC_KEYS = ("topic", "category", "label_topic", "tag")
_RATIONALE_KEYS = ("rationale", "reason", "explanation", "notes")


def _uid(prefix: str = "upl_") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _pick(d: dict, keys: tuple[str, ...]) -> str:
    for k in keys:
        if k in d and d[k] is not None:
            v = d[k]
            if isinstance(v, (list, dict)):
                # ShareGPT-style messages
                if k == "messages" and isinstance(v, list):
                    return _messages_to_io(v)[0]
                continue
            s = str(v).strip()
            if s:
                return s
    # case-insensitive
    low = {str(k).lower(): v for k, v in d.items()}
    for k in keys:
        if k in low and low[k] is not None and not isinstance(low[k], (list, dict)):
            s = str(low[k]).strip()
            if s:
                return s
    return ""


def _messages_to_io(messages: list) -> tuple[str, str]:
    user_parts: list[str] = []
    asst_parts: list[str] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or m.get("from") or "").lower()
        content = str(m.get("content") or m.get("value") or m.get("text") or "").strip()
        if not content:
            continue
        if role in {"user", "human", "customer"}:
            user_parts.append(content)
        elif role in {"assistant", "gpt", "bot", "model"}:
            asst_parts.append(content)
    return ("\n".join(user_parts).strip(), "\n".join(asst_parts).strip())


def _row_from_dict(d: dict, default_topic: str) -> dict[str, Any] | None:
    if not isinstance(d, dict):
        return None
    # ShareGPT / chat
    if isinstance(d.get("messages"), list):
        inp, out = _messages_to_io(d["messages"])
    else:
        inp = _pick(d, _INPUT_KEYS)
        out = _pick(d, _OUTPUT_KEYS)
        # Alpaca
        if not inp and d.get("instruction"):
            inst = str(d.get("instruction") or "").strip()
            ctx = str(d.get("input") or "").strip()
            inp = f"{inst}\n\n{ctx}".strip() if ctx else inst
            out = str(d.get("output") or d.get("response") or "").strip()
    if not inp or not out:
        return None
    topic = _pick(d, _TOPIC_KEYS) or default_topic
    topic = re.sub(r"[^a-zA-Z0-9_\-]+", "_", topic.lower())[:80] or "user_upload"
    rationale = _pick(d, _RATIONALE_KEYS) or "User-uploaded labeled example"
    difficulty = str(d.get("difficulty") or "moderate")[:40]
    is_neg = bool(d.get("is_negative") or d.get("negative") or False)
    return {
        "topic": topic,
        "input": inp[:8000],
        "output": out[:8000],
        "rationale": rationale[:2000],
        "difficulty": difficulty,
        "is_negative": is_neg,
    }


def _parse_json_bytes(raw: bytes, default_topic: str) -> list[dict]:
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return []
    out: list[dict] = []
    # JSONL
    if "\n" in text and not text.lstrip().startswith("["):
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            row = _row_from_dict(obj, default_topic) if isinstance(obj, dict) else None
            if row:
                out.append(row)
        if out:
            return out
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return out
    if isinstance(data, list):
        for obj in data:
            if isinstance(obj, dict):
                row = _row_from_dict(obj, default_topic)
                if row:
                    out.append(row)
    elif isinstance(data, dict):
        # { "examples": [...] } or single row
        if isinstance(data.get("examples"), list):
            for obj in data["examples"]:
                if isinstance(obj, dict):
                    row = _row_from_dict(obj, default_topic)
                    if row:
                        out.append(row)
        elif isinstance(data.get("data"), list):
            for obj in data["data"]:
                if isinstance(obj, dict):
                    row = _row_from_dict(obj, default_topic)
                    if row:
                        out.append(row)
        else:
            row = _row_from_dict(data, default_topic)
            if row:
                out.append(row)
    return out


def _parse_csv_bytes(raw: bytes, default_topic: str) -> list[dict]:
    text = raw.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    out: list[dict] = []
    for obj in reader:
        if not isinstance(obj, dict):
            continue
        # normalize keys
        norm = {str(k).strip().lower(): v for k, v in obj.items() if k is not None}
        row = _row_from_dict(norm, default_topic)
        if row:
            out.append(row)
    return out


def extract_pairs_from_zip(
    fileobj: BinaryIO,
    *,
    default_topic: str = "user_upload",
    max_rows: int = MAX_ROWS_PER_UPLOAD,
) -> tuple[list[dict], dict[str, Any]]:
    """
    Read a zip archive and extract input/output training pairs.
    Returns (rows, meta).
    """
    files_seen = 0
    files_used = 0
    skipped_files: list[str] = []
    rows: list[dict] = []

    try:
        zf = zipfile.ZipFile(fileobj)
    except zipfile.BadZipFile as e:
        raise ValueError("Not a valid zip file") from e

    with zf:
        names = [n for n in zf.namelist() if not n.endswith("/") and not n.startswith("__MACOSX")]
        for name in names[:MAX_FILES_SCANNED]:
            files_seen += 1
            lower = name.lower()
            if not any(lower.endswith(ext) for ext in (".jsonl", ".json", ".csv", ".txt")):
                skipped_files.append(name)
                continue
            try:
                raw = zf.read(name)
            except Exception:  # noqa: BLE001
                skipped_files.append(name)
                continue
            if len(raw) > 8 * 1024 * 1024:
                skipped_files.append(name)
                continue
            parsed: list[dict] = []
            if lower.endswith(".csv"):
                parsed = _parse_csv_bytes(raw, default_topic)
            elif lower.endswith((".jsonl", ".json", ".txt")):
                parsed = _parse_json_bytes(raw, default_topic)
            if parsed:
                files_used += 1
                rows.extend(parsed)
            else:
                skipped_files.append(name)
            if len(rows) >= max_rows:
                rows = rows[:max_rows]
                break

    meta = {
        "files_seen": files_seen,
        "files_used": files_used,
        "skipped_files": skipped_files[:30],
        "pairs_found": len(rows),
        "truncated": len(rows) >= max_rows,
    }
    return rows, meta


def import_zip_as_gold(
    db: Session,
    *,
    owner_user_id: str,
    tenant_id: str,
    fileobj: BinaryIO,
    filename: str = "upload.zip",
    default_topic: str = "user_upload",
    enforce_cap: bool = True,
) -> dict[str, Any]:
    """
    Parse zip → GoldExample rows with source_kind=user_upload.
    Stored in gold format for download + future Double Helix training.
    """
    batch_id = _uid("uby_")
    rows, meta = extract_pairs_from_zip(fileobj, default_topic=default_topic)
    if not rows:
        return {
            "ok": False,
            "error": (
                "No training pairs found. Zip should contain .jsonl / .json / .csv "
                "with input+output (or question+answer / prompt+completion) fields."
            ),
            "meta": meta,
            "created": 0,
            "upload_batch_id": batch_id,
        }

    created = 0
    skipped = 0
    samples: list[dict] = []
    for i, row in enumerate(rows):
        g = add_gold_example(
            db,
            owner_user_id=owner_user_id,
            tenant_id=tenant_id,
            topic=row["topic"],
            input_text=row["input"],
            output_text=row["output"],
            rationale=row.get("rationale"),
            difficulty=row.get("difficulty") or "moderate",
            is_negative=bool(row.get("is_negative")),
            source_kind=USER_UPLOAD_SOURCE_KIND,
            source_ref=f"user_upload:{batch_id}:{i}",
            verification_status="verified",
            metadata={
                "origin": "user_upload",
                "upload_batch_id": batch_id,
                "upload_filename": (filename or "upload.zip")[:200],
                "for_double_helix": True,
                "is_seed": False,
            },
            enforce_cap=enforce_cap,
            skip_near_duplicate=True,
        )
        if g is None:
            skipped += 1
            continue
        created += 1
        if len(samples) < 3:
            samples.append(gold_to_dict(g))

    return {
        "ok": True,
        "created": created,
        "skipped": skipped,
        "upload_batch_id": batch_id,
        "source_kind": USER_UPLOAD_SOURCE_KIND,
        "for_double_helix": True,
        "meta": meta,
        "samples": samples,
        "message": (
            f"Saved {created} gold-format example(s) from your zip "
            f"(batch {batch_id}). Download anytime from My data · Export my uploads. "
            "These rows are ready for Double Helix training later."
        ),
    }


def count_user_upload_gold(
    db: Session, owner_user_id: str, tenant_id: str
) -> int:
    return (
        db.query(m.GoldExample)
        .filter_by(
            owner_user_id=owner_user_id,
            tenant_id=tenant_id,
            is_archived=False,
            source_kind=USER_UPLOAD_SOURCE_KIND,
        )
        .count()
    )
