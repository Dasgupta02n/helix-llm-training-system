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
    # Preserve blank lines / question boundaries; only collapse runs of spaces/tabs
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()[:limit]


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


def write_corpus_units_as_gold(
    db: Session,
    *,
    tenant_id: str,
    owner_user_id: str,
    brief: dict[str, Any],
    units: list[dict[str, Any]],
    batch_size: int = 5,
    tenant: Any | None = None,
) -> dict[str, Any]:
    """
    Hand-off: corpus training units → GoldExample rows.

    Separate from the generic campaign/candidate path so a verified corpus
    campaign cannot "succeed" while gold is silently skipped by near-dup
    or rejected-ref bookkeeping.
    """
    from helix.services.gold_quality import synthesize_gold_pair
    from helix.services.library import (
        add_gold_example,
        count_gold_toward_cap,
        get_or_create_scope,
    )

    made = 0
    skipped = 0
    rejected = 0
    errors: list[str] = []
    created_ids: list[str] = []
    details: list[dict[str, Any]] = []
    scope = get_or_create_scope(db, owner_user_id, tenant_id)

    known_ids = {
        r[0]
        for r in db.query(m.GoldExample.id)
        .filter_by(
            owner_user_id=owner_user_id,
            tenant_id=tenant_id,
            is_archived=False,
        )
        .all()
    }

    existing_refs = {
        r[0]
        for r in db.query(m.GoldExample.source_ref)
        .filter_by(
            owner_user_id=owner_user_id,
            tenant_id=tenant_id,
            is_archived=False,
        )
        .filter(m.GoldExample.source_ref.isnot(None))
        .filter(m.GoldExample.verification_status != "rejected")
        .all()
        if r[0]
    }

    for unit in units:
        if made >= batch_size:
            break
        ref = str(unit.get("source_ref") or f"corpus:{unit.get('corpus_id')}")[:64]
        if ref in existing_refs:
            skipped += 1
            details.append({"source_ref": ref, "status": "already_promoted"})
            continue
        title = unit.get("title") or "Corpus document"
        evidence = (unit.get("evidence") or "").strip()
        category = unit.get("category") or "general"
        if len(evidence) < 40:
            rejected += 1
            details.append(
                {"source_ref": ref, "status": "rejected", "reasons": ["evidence_too_short"]}
            )
            continue
        topic = re.sub(r"[^a-z0-9]+", "_", category.lower()).strip("_") or "general"

        # Template-first corpus synth with one retry (content gates are deterministic;
        # retry re-runs after a short backoff in case of transient import/state glitches).
        pair = None
        last_reasons: list[str] = ["synth_failed"]
        for attempt in range(2):
            try:
                pair = synthesize_gold_pair(
                    brief=brief,
                    title=title,
                    evidence=evidence,
                    topic=topic,
                    url=unit.get("url") or "",
                    tenant=tenant,
                    prefer_llm=False,
                )
            except Exception as e:  # noqa: BLE001
                last_reasons = [f"synth_exception:{type(e).__name__}:{e}"]
                pair = None
                if attempt == 0:
                    import time

                    time.sleep(0.15)
                continue
            if pair and pair.get("quality_ok"):
                break
            last_reasons = list(
                (pair or {}).get("reject_reasons")
                or (pair or {}).get("template_reject_reasons")
                or ["synth_failed"]
            )
            if attempt == 0:
                import time

                time.sleep(0.15)

        if not pair or not pair.get("quality_ok"):
            rejected += 1
            details.append(
                {
                    "source_ref": ref,
                    "status": "rejected",
                    "reasons": last_reasons,
                    "synth_status": "synth_failed",
                    "title": title[:80],
                    "topic": topic,
                    "evidence_len": len(evidence),
                    "note": (
                        "Synthesis failed quality gates (not a timeout unless "
                        "synth_exception:*). Reasons list the gate(s)."
                    ),
                }
            )
            continue

        verified_toward_cap = count_gold_toward_cap(
            db, owner_user_id=owner_user_id, tenant_id=tenant_id
        )
        at_cap = verified_toward_cap >= int(scope.gold_target_count or 0)

        try:
            g = add_gold_example(
                db,
                owner_user_id=owner_user_id,
                tenant_id=tenant_id,
                topic=topic,
                input_text=pair["input"][:4000],
                output_text=pair["output"][:2000],
                rationale=(pair.get("rationale") or "Corpus FAQ → support gold")[:1000],
                difficulty=pair.get("difficulty") or "moderate",
                is_negative=False,
                source_kind="corpus",
                source_ref=ref,
                verification_status="verified",
                enforce_cap=True,
                skip_near_duplicate=True,
                metadata={
                    "from": "user_corpus",
                    "corpus_id": unit.get("corpus_id"),
                    "candidate_id": unit.get("candidate_id"),
                    "category": category,
                    "title": title,
                    "synth": pair.get("synth"),
                    "domain": brief.get("domain"),
                },
            )
        except Exception as e:  # noqa: BLE001
            errors.append(f"{ref}:{e}")
            details.append({"source_ref": ref, "status": "error", "error": str(e)[:200]})
            continue

        if not g:
            # Distinct statuses so ops can tell goal-cap vs unexpected null
            if at_cap:
                skipped += 1
                details.append(
                    {
                        "source_ref": ref,
                        "status": "goal_cap_reached",
                        "verified_count": verified_toward_cap,
                        "gold_target": scope.gold_target_count,
                        "note": (
                            "Gold goal already met by verified rows only "
                            "(rejected rows do not count toward the goal)."
                        ),
                    }
                )
            else:
                rejected += 1
                details.append(
                    {
                        "source_ref": ref,
                        "status": "write_returned_null",
                        "verified_count": verified_toward_cap,
                        "gold_target": scope.gold_target_count,
                        "note": (
                            "add_gold_example returned None without being at verified cap "
                            "(possible exact-dup race or unexpected filter)."
                        ),
                    }
                )
            continue

        if g.id in known_ids:
            skipped += 1
            details.append({"source_ref": ref, "status": "existing_row", "id": g.id})
            continue

        known_ids.add(g.id)
        existing_refs.add(ref)
        created_ids.append(g.id)
        made += 1
        details.append(
            {
                "source_ref": ref,
                "status": "created",
                "id": g.id,
                "source_kind": g.source_kind,
            }
        )
        cat_row = (
            db.query(m.CategoryState)
            .filter_by(tenant_id=tenant_id, name=category)
            .first()
        )
        if cat_row:
            cat_row.verified_count = int(cat_row.verified_count or 0) + 1

    return {
        "ok": True,
        "corpus_gold_new": made,
        "corpus_gold_skipped": skipped,
        "corpus_gold_rejected": rejected,
        "errors": errors,
        "details": details,
        "created_ids": created_ids,
    }


def extract_training_units(
    *,
    title: str,
    content: str,
    category: str = "general",
) -> list[dict[str, str]]:
    """
    Split a pasted FAQ / policy doc into training units (title + evidence body).
    Handles Q1/Q2, 'Question N', numbered lists, or falls back to whole doc.
    Works even if newlines were collapsed to spaces.
    """
    text = (content or "").strip()
    if not text:
        return []
    # Prefer splitting on explicit Q markers (line-start OR mid-string after collapse)
    chunks = re.split(
        r"(?=(?:^|[\n\r]|\s)(?:Q(?:uestion)?\s*\d+\s*[:.)\-]|#{1,3}\s+\S|\d{1,2}[\.)]\s+[A-Z]))",
        text,
        flags=re.I | re.M,
    )
    # Also try strict Qn: split if still a single blob
    if len([c for c in chunks if len((c or "").strip()) >= 40]) < 2:
        chunks = re.split(r"(?=(?:Q(?:uestion)?\s*\d+\s*[:.)\-]))", text, flags=re.I)
    units: list[dict[str, str]] = []
    for raw in chunks:
        part = re.sub(r"[ \t]+", " ", (raw or "").strip())
        part = re.sub(r"\n{2,}", "\n", part).strip()
        if len(part) < 40:
            continue
        # first sentence / line as local title
        m = re.match(
            r"^(?:Q(?:uestion)?\s*\d*\s*[:.)\-]?\s*)?(.+?)(?:\?|\.|$)",
            part,
            re.I,
        )
        local_title = (m.group(1) if m else part[:80]).strip()
        if not local_title.endswith("?") and "?" in part[:160]:
            local_title = part[: part.index("?") + 1]
        units.append(
            {
                "title": (local_title or title or "Corpus Q&A")[:200],
                "evidence": part[:4000],
                "category": category or "general",
            }
        )
    if not units:
        flat = re.sub(r"\s+", " ", text)
        units.append(
            {
                "title": (title or "Corpus document")[:200],
                "evidence": flat[:4000],
                "category": category or "general",
            }
        )
    # Cap units per document to avoid explosion
    return units[:12]


def infer_category_from_text(text: str, brief_categories: list[str]) -> str:
    """Map corpus content to the closest active plan category."""
    low = (text or "").lower()
    cats = [str(c) for c in (brief_categories or []) if str(c).strip()]
    if not cats:
        return "general"
    best = cats[0]
    best_score = -1
    for c in cats:
        tokens = re.findall(r"[a-z0-9]{3,}", c.lower())
        score = sum(1 for t in tokens if t in low)
        # synonym boosts for common support categories
        if "late" in c.lower() and any(x in low for x in ("late", "delay", "eta")):
            score += 2
        if "missing" in c.lower() and "missing" in low:
            score += 2
        if "wrong" in c.lower() and any(x in low for x in ("wrong", "incorrect")):
            score += 2
        if "refund" in c.lower() and "refund" in low:
            score += 2
        if "driver" in c.lower() and "driver" in low:
            score += 2
        if "account" in c.lower() and "account" in low:
            score += 2
        if "damag" in low and "damag" in c.lower():
            score += 2
        if score > best_score:
            best_score = score
            best = c
    return best


def promote_corpus_into_pipeline(
    db: Session,
    *,
    tenant_id: str,
    owner_user_id: str | None,
    brief: dict[str, Any] | None = None,
    batch_size: int = 5,
) -> dict[str, Any]:
    """
    Turn active corpus docs into DiscoveryCandidate + EvidenceStaging rows
    so judge agents and gold promotion both see full user-supplied text
    (not title/snippet web scraps).
    """
    brief = brief or {}
    cats = [str(c) for c in (brief.get("categories") or []) if str(c).strip()]
    docs = list_corpus(
        db, tenant_id=tenant_id, owner_user_id=owner_user_id, limit=max(batch_size * 3, 10)
    )
    candidates_created = 0
    evidence_written = 0
    units_out: list[dict[str, Any]] = []

    for doc in docs:
        units = extract_training_units(
            title=doc.title or "Corpus document",
            content=doc.content_text or "",
            category=doc.category or "general",
        )
        for i, unit in enumerate(units):
            cat = infer_category_from_text(
                unit["evidence"], cats
            ) if cats else (unit.get("category") or "general")
            # Stable synthetic URL so write_discovery_candidate path can dedupe
            pseudo_url = f"corpus://{doc.id}/u{i}"
            existing = (
                db.query(m.DiscoveryCandidate)
                .filter_by(tenant_id=tenant_id, url=pseudo_url)
                .first()
            )
            if existing:
                cand_id = existing.id
            else:
                cand_id = _uid("cand_")
                db.add(
                    m.DiscoveryCandidate(
                        id=cand_id,
                        tenant_id=tenant_id,
                        category=cat,
                        source="corpus",
                        title=unit["title"][:500],
                        url=pseudo_url,
                        brand=(brief.get("domain") or "")[:200] or None,
                        creator=None,
                        relevance_score=0.95,
                        status="pending",
                    )
                )
                candidates_created += 1
            # Always refresh staging with full corpus body (agents + gold read this)
            staging = (
                db.query(m.EvidenceStaging)
                .filter_by(tenant_id=tenant_id, candidate_id=cand_id)
                .order_by(m.EvidenceStaging.created_at.desc())
                .first()
            )
            body = unit["evidence"]
            signals = json.dumps(
                {
                    "domain": brief.get("domain"),
                    "corpus_id": doc.id,
                    "source": "corpus",
                    "title": unit["title"],
                    "url": pseudo_url,
                }
            )
            stg_id: str | None = staging.id if staging else None
            if staging:
                staging.content_text = body
                staging.preliminary_confidence = 0.92
                staging.identity_signals_json = signals
                staging.brand = (brief.get("domain") or "")[:200] or None
            else:
                stg_id = _uid("stg_")
                db.add(
                    m.EvidenceStaging(
                        id=stg_id,
                        tenant_id=tenant_id,
                        candidate_id=cand_id,
                        content_text=body,
                        preliminary_confidence=0.92,
                        identity_signals_json=signals,
                        brand=(brief.get("domain") or "")[:200] or None,
                        status="pending_dedup",
                    )
                )
                evidence_written += 1
            # Mark candidate staged so collection agents don't re-fetch empty scrapes
            cand_row = (
                db.query(m.DiscoveryCandidate)
                .filter_by(id=cand_id, tenant_id=tenant_id)
                .first()
            )
            if cand_row:
                cand_row.status = "staged"
                cand_row.category = cat
                cand_row.title = unit["title"][:500]
            # Verified campaign stub so knowledge_extraction/graph see full corpus text
            # (not title-only web scrapes). Idempotent via title prefix.
            camp_title = f"[corpus] {unit['title']}"[:500]
            camp = (
                db.query(m.Campaign)
                .filter_by(tenant_id=tenant_id, title=camp_title)
                .first()
            )
            if not camp:
                camp_id = _uid("camp_")
                db.add(
                    m.Campaign(
                        id=camp_id,
                        tenant_id=tenant_id,
                        brand=(brief.get("domain") or "")[:200] or None,
                        creator=None,
                        category=cat,
                        title=camp_title,
                        verification_status="verified",
                        confidence=0.92,
                        verification_reasoning=(
                            "User-supplied corpus document — treated as verified "
                            "evidence for domain mining (full text staged)."
                        ),
                    )
                )
                db.add(
                    m.CampaignEvidence(
                        id=_uid("ce_"),
                        tenant_id=tenant_id,
                        campaign_id=camp_id,
                        staging_id=stg_id,
                        source="corpus",
                        content_text=body,
                        confidence=0.92,
                    )
                )
            else:
                camp.verification_status = "verified"
                camp.category = cat
                camp.confidence = max(float(camp.confidence or 0), 0.92)
                # Refresh evidence text on re-run
                ce = (
                    db.query(m.CampaignEvidence)
                    .filter_by(tenant_id=tenant_id, campaign_id=camp.id)
                    .first()
                )
                if ce:
                    ce.content_text = body
                    ce.confidence = 0.92

            units_out.append(
                {
                    "corpus_id": doc.id,
                    "candidate_id": cand_id,
                    "title": unit["title"],
                    "category": cat,
                    "evidence": body,
                    "url": pseudo_url,
                    "source_ref": f"corpus:{doc.id}:u{i}",
                }
            )
            if len(units_out) >= batch_size * 3:
                break
        if len(units_out) >= batch_size * 3:
            break

    db.flush()
    return {
        "ok": True,
        "docs": len(docs),
        "candidates_created": candidates_created,
        "evidence_written": evidence_written,
        "units": units_out,
    }
