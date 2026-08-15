"""Zip packs of user-generated training rows (gold / synth / corpus).

A pack is one zip with separate JSONL files. Scope is either the whole
account library or the current Riu session (rows created after that chat
started). Named saves store the row ids so a pack can be downloaded later.
"""

from __future__ import annotations

import io
import json
import re
import uuid
import zipfile
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from helix.db import models as m
from helix.services.library import (
    _is_seed_kind,
    gold_to_chat_messages,
    gold_to_dict,
    synthetic_to_dict,
)
from helix.services.user_gold_upload import USER_UPLOAD_SOURCE_KIND
from helix.services.user_material_upload import USER_MATERIAL_SOURCE_KIND

PACK_KIND = "library_pack"
BUCKETS = ("gold", "synthetic", "structured", "unstructured")
FILE_NAMES = {
    "gold": "gold.jsonl",
    "synthetic": "synthetic.jsonl",
    "structured": "corpus_structured.jsonl",
    "unstructured": "corpus_unstructured.jsonl",
}
STRUCTURED_KINDS = {USER_UPLOAD_SOURCE_KIND, "byo", "upload"}
UNSTRUCTURED_KINDS = {USER_MATERIAL_SOURCE_KIND, "material", "materials"}


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _uid(prefix: str = "ds_") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def classify_gold(g: m.GoldExample, *, tenant_slug: str | None = None) -> str | None:
    """Return zip bucket, or None to skip (seed / rejected / archived)."""
    if g.is_archived:
        return None
    if (g.verification_status or "").lower() == "rejected":
        return None
    if _is_seed_kind(
        g.source_kind,
        g.topic,
        input_text=g.input_text,
        metadata_json=g.metadata_json,
        source_ref=g.source_ref,
        created_at=g.created_at,
        tenant_slug=tenant_slug,
    ):
        return None
    sk = (g.source_kind or "").lower()
    if sk in STRUCTURED_KINDS:
        return "structured"
    if sk in UNSTRUCTURED_KINDS:
        return "unstructured"
    return "gold"


def current_session_since(
    db: Session, *, user_id: str, tenant_id: str
) -> tuple[datetime | None, str | None]:
    """Active Riu chat start, else the most recent chat. None if they never used Riu."""
    row = (
        db.query(m.RiuSession)
        .filter_by(owner_user_id=user_id, tenant_id=tenant_id, status="active")
        .order_by(m.RiuSession.updated_at.desc())
        .first()
    )
    if not row:
        row = (
            db.query(m.RiuSession)
            .filter_by(owner_user_id=user_id, tenant_id=tenant_id)
            .order_by(m.RiuSession.updated_at.desc())
            .first()
        )
    if not row:
        return None, None
    return _aware(row.created_at), row.id


def collect_library_buckets(
    db: Session,
    *,
    user_id: str,
    tenant_id: str,
    tenant_slug: str | None = None,
    since: datetime | None = None,
    id_filter: dict[str, list[str]] | None = None,
) -> dict[str, list[dict[str, Any]]]:
    buckets: dict[str, list[dict[str, Any]]] = {k: [] for k in BUCKETS}
    since_a = _aware(since)

    gold_q = db.query(m.GoldExample).filter_by(
        owner_user_id=user_id, tenant_id=tenant_id, is_archived=False
    )
    if since_a is not None:
        gold_q = gold_q.filter(m.GoldExample.created_at >= since_a)
    gold_rows = gold_q.order_by(m.GoldExample.created_at.asc()).all()
    allowed_gold = None
    if id_filter is not None:
        allowed_gold = set(
            (id_filter.get("gold") or [])
            + (id_filter.get("structured") or [])
            + (id_filter.get("unstructured") or [])
        )
    for g in gold_rows:
        if allowed_gold is not None and g.id not in allowed_gold:
            continue
        bucket = classify_gold(g, tenant_slug=tenant_slug)
        if not bucket:
            continue
        buckets[bucket].append(gold_to_dict(g, tenant_slug=tenant_slug))

    syn_q = db.query(m.SyntheticExample).filter_by(
        owner_user_id=user_id, tenant_id=tenant_id, is_archived=False
    )
    if since_a is not None:
        syn_q = syn_q.filter(m.SyntheticExample.created_at >= since_a)
    allowed_syn = set(id_filter.get("synthetic") or []) if id_filter is not None else None
    for s in syn_q.order_by(m.SyntheticExample.created_at.asc()).all():
        if allowed_syn is not None and s.id not in allowed_syn:
            continue
        buckets["synthetic"].append(synthetic_to_dict(s))
    return buckets


def _training_line(row: dict[str, Any], *, fmt: str) -> str:
    if fmt == "chat":
        return json.dumps(
            gold_to_chat_messages(
                row.get("input") or row.get("input_text") or "",
                row.get("output") or row.get("output_text") or "",
            ),
            ensure_ascii=False,
        )
    slim = {
        "id": row.get("id"),
        "topic": row.get("topic"),
        "input": row.get("input") or row.get("input_text") or "",
        "output": row.get("output") or row.get("output_text") or "",
        "rationale": row.get("rationale"),
        "difficulty": row.get("difficulty"),
        "is_negative": row.get("is_negative"),
        "source_kind": row.get("source_kind"),
        "kind": row.get("kind"),
    }
    return json.dumps(slim, ensure_ascii=False)


def _readme(scope: str, counts: dict[str, int], since_iso: str | None) -> str:
    lines = [
        "Helix training pack",
        f"Scope: {scope}",
    ]
    if since_iso:
        lines.append(f"Session started: {since_iso}")
    lines.extend(
        [
            "",
            "Each file is JSONL (one training row per line).",
            f"- {FILE_NAMES['gold']} — generated gold ({counts.get('gold', 0)})",
            f"- {FILE_NAMES['synthetic']} — synthetic variations ({counts.get('synthetic', 0)})",
            f"- {FILE_NAMES['structured']} — labeled uploads in training format ({counts.get('structured', 0)})",
            f"- {FILE_NAMES['unstructured']} — materials converted to training format ({counts.get('unstructured', 0)})",
            "",
            "Empty kinds are omitted.",
        ]
    )
    return "\n".join(lines) + "\n"


def build_library_zip(
    buckets: dict[str, list[dict[str, Any]]],
    *,
    scope: str,
    fmt: str = "jsonl",
    since_iso: str | None = None,
) -> tuple[bytes, dict[str, int]]:
    counts = {k: len(buckets.get(k) or []) for k in BUCKETS}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("README.txt", _readme(scope, counts, since_iso))
        zf.writestr("manifest.json", json.dumps({"scope": scope, "counts": counts}, indent=2))
        for key in BUCKETS:
            rows = buckets.get(key) or []
            if not rows:
                continue
            body = "\n".join(_training_line(r, fmt=fmt) for r in rows) + "\n"
            zf.writestr(FILE_NAMES[key], body)
    return buf.getvalue(), counts


def pack_for_user(
    db: Session,
    *,
    user_id: str,
    tenant_id: str,
    tenant_slug: str,
    scope: str = "library",
    fmt: str = "jsonl",
    id_filter: dict[str, list[str]] | None = None,
) -> tuple[bytes, dict[str, Any]]:
    since = None
    session_id = None
    if scope == "session":
        since, session_id = current_session_since(db, user_id=user_id, tenant_id=tenant_id)
        if since is None and id_filter is None:
            empty = {k: [] for k in BUCKETS}
            raw, counts = build_library_zip(empty, scope="current session", fmt=fmt)
            return raw, {
                "scope": "session",
                "session_id": None,
                "since": None,
                "counts": counts,
                "empty_reason": "No current chat session. Open Riu or upload data first.",
            }
    buckets = collect_library_buckets(
        db,
        user_id=user_id,
        tenant_id=tenant_id,
        tenant_slug=tenant_slug,
        since=since if scope == "session" else None,
        id_filter=id_filter,
    )
    label = "current session" if scope == "session" else "full library"
    since_iso = since.isoformat() if since else None
    raw, counts = build_library_zip(buckets, scope=label, fmt=fmt, since_iso=since_iso)
    return raw, {
        "scope": scope,
        "session_id": session_id,
        "since": since_iso,
        "counts": counts,
        "ids": {k: [r["id"] for r in buckets[k]] for k in BUCKETS},
    }


def save_session_pack(
    db: Session,
    *,
    user: m.User,
    tenant: m.Tenant,
    version: str,
) -> dict[str, Any]:
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", (version or "").strip())[:40]
    if not safe:
        raise ValueError("Name this version first (letters, numbers, dash, underscore).")
    existing = (
        db.query(m.DatasetVersion)
        .filter_by(tenant_id=tenant.id, version=safe)
        .first()
    )
    if existing:
        raise ValueError("That name is already used. Pick another.")
    _raw, meta = pack_for_user(
        db,
        user_id=user.id,
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
        scope="session",
    )
    counts = meta.get("counts") or {}
    if meta.get("empty_reason"):
        raise ValueError(meta["empty_reason"])
    if sum(int(counts.get(k) or 0) for k in BUCKETS) <= 0:
        raise ValueError(
            "Nothing in this chat yet. Generate gold, synthetics, or upload corpus first."
        )
    manifest = {
        "kind": PACK_KIND,
        "scope": "session",
        "session_id": meta.get("session_id"),
        "since": meta.get("since"),
        "counts": counts,
        "ids": meta.get("ids") or {},
        "created_by": user.email,
        "source": "current_session",
    }
    row = m.DatasetVersion(
        id=_uid(),
        tenant_id=tenant.id,
        version=safe,
        manifest_json=json.dumps(manifest),
    )
    db.add(row)
    db.commit()
    return {
        "ok": True,
        "id": row.id,
        "version": safe,
        "count": sum(int(counts.get(k) or 0) for k in BUCKETS),
        "counts": counts,
        "manifest": manifest,
        "empty_reason": meta.get("empty_reason"),
    }


def load_pack_manifest(row: m.DatasetVersion) -> dict[str, Any] | None:
    try:
        data = json.loads(row.manifest_json or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("kind") != PACK_KIND:
        return None
    return data


def zip_saved_pack(
    db: Session,
    *,
    user_id: str,
    tenant: m.Tenant,
    version: str,
    fmt: str = "jsonl",
) -> tuple[bytes, dict[str, Any]]:
    row = (
        db.query(m.DatasetVersion)
        .filter_by(tenant_id=tenant.id, version=version)
        .first()
    )
    if not row:
        raise ValueError("Saved pack not found")
    manifest = load_pack_manifest(row)
    if not manifest:
        raise ValueError("That saved version is not a library pack")
    raw, meta = pack_for_user(
        db,
        user_id=user_id,
        tenant_id=tenant.id,
        tenant_slug=tenant.slug,
        scope="library",
        fmt=fmt,
        id_filter=manifest.get("ids") if isinstance(manifest.get("ids"), dict) else {},
    )
    meta["version"] = version
    meta["scope"] = "saved"
    return raw, meta


def pack_filename(slug: str, label: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_.-]+", "_", f"{slug}_{label}")[:80]
    return f"helix_{safe}.zip"
