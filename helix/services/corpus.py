"""Bring-your-own corpus: user-supplied docs/URLs as primary evidence."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from helix.db import models as m


def _uid(prefix: str = "corp_") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", errors="ignore")).hexdigest()


def _clean(text: str, limit: int = 50_000) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    return t[:limit]


def document_to_dict(row: m.CorpusDocument) -> dict[str, Any]:
    return {
        "id": row.id,
        "title": row.title,
        "url": row.url,
        "content_text": (row.content_text or "")[:2000],
        "content_length": len(row.content_text or ""),
        "source_kind": row.source_kind,
        "category": row.category,
        "status": row.status,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def list_corpus(
    db: Session,
    *,
    tenant_id: str,
    owner_user_id: str | None = None,
    limit: int = 50,
) -> list[m.CorpusDocument]:
    q = (
        db.query(m.CorpusDocument)
        .filter_by(tenant_id=tenant_id, status="active")
        .order_by(m.CorpusDocument.created_at.desc())
    )
    if owner_user_id:
        q = q.filter(
            (m.CorpusDocument.owner_user_id == owner_user_id)
            | (m.CorpusDocument.owner_user_id.is_(None))
        )
    return q.limit(limit).all()


def add_paste(
    db: Session,
    *,
    tenant_id: str,
    owner_user_id: str | None,
    title: str,
    content: str,
    category: str = "general",
) -> dict[str, Any]:
    body = _clean(content)
    if len(body) < 40:
        return {"ok": False, "error": "Paste at least ~40 characters of useful content."}
    h = _hash_text(body)
    existing = (
        db.query(m.CorpusDocument)
        .filter_by(tenant_id=tenant_id, content_hash=h)
        .first()
    )
    if existing:
        return {"ok": True, "duplicate": True, "document": document_to_dict(existing)}
    row = m.CorpusDocument(
        id=_uid(),
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        title=(title or "Pasted document")[:500],
        url=None,
        content_text=body,
        source_kind="paste",
        content_hash=h,
        category=(category or "general")[:120],
        status="active",
        metadata_json=json.dumps({"origin": "user_paste"}),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"ok": True, "document": document_to_dict(row)}


def add_url(
    db: Session,
    *,
    tenant_id: str,
    owner_user_id: str | None,
    url: str,
    title: str = "",
    category: str = "general",
    fetch: bool = True,
) -> dict[str, Any]:
    url = (url or "").strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        return {"ok": False, "error": "URL must start with http:// or https://"}
    parsed = urlparse(url)
    if not parsed.netloc:
        return {"ok": False, "error": "Invalid URL"}

    body = ""
    page_title = title
    if fetch:
        try:
            from helix.services.gather import apify_client

            item, _meta = apify_client.fetch_page(url)
            body = _clean(item.get("text") or item.get("content") or "")
            page_title = page_title or (item.get("title") or "")
        except Exception as e:  # noqa: BLE001
            # Fallback: store URL with placeholder; user can paste content later
            return {
                "ok": False,
                "error": f"Could not fetch URL content: {e}. Paste the text instead.",
            }

    if len(body) < 40:
        return {
            "ok": False,
            "error": "Fetched page had too little text. Paste the document content instead.",
        }

    h = _hash_text(body)
    existing = (
        db.query(m.CorpusDocument)
        .filter_by(tenant_id=tenant_id, content_hash=h)
        .first()
    )
    if existing:
        return {"ok": True, "duplicate": True, "document": document_to_dict(existing)}

    host = parsed.netloc
    row = m.CorpusDocument(
        id=_uid(),
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        title=(page_title or f"Doc from {host}")[:500],
        url=url[:1000],
        content_text=body,
        source_kind="url",
        content_hash=h,
        category=(category or "general")[:120],
        status="active",
        metadata_json=json.dumps({"origin": "user_url", "host": host}),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"ok": True, "document": document_to_dict(row)}


def archive_document(
    db: Session, *, tenant_id: str, doc_id: str, owner_user_id: str | None
) -> dict[str, Any]:
    row = (
        db.query(m.CorpusDocument)
        .filter_by(id=doc_id, tenant_id=tenant_id)
        .first()
    )
    if not row:
        return {"ok": False, "error": "not found"}
    if owner_user_id and row.owner_user_id and row.owner_user_id != owner_user_id:
        return {"ok": False, "error": "not your document"}
    row.status = "archived"
    row.updated_at = _now()
    db.commit()
    return {"ok": True}


def corpus_count(db: Session, tenant_id: str) -> int:
    return (
        db.query(m.CorpusDocument)
        .filter_by(tenant_id=tenant_id, status="active")
        .count()
    )
