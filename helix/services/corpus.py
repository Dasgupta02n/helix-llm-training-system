"""Bring-your-own corpus: user-supplied docs/URLs as plan-scoped evidence."""

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


def _meta(row: m.CorpusDocument) -> dict[str, Any]:
    try:
        d = json.loads(row.metadata_json or "{}")
        return d if isinstance(d, dict) else {}
    except json.JSONDecodeError:
        return {}


def document_to_dict(row: m.CorpusDocument) -> dict[str, Any]:
    meta = _meta(row)
    return {
        "id": row.id,
        "title": row.title,
        "url": row.url,
        "content_text": (row.content_text or "")[:2000],
        "content_length": len(row.content_text or ""),
        "source_kind": row.source_kind,
        "category": row.category,
        "status": row.status,
        "project_id": row.project_id or meta.get("project_id"),
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def active_project_id(db: Session, tenant_id: str) -> str | None:
    from helix.services.brief import get_active_project

    p = get_active_project(db, tenant_id)
    return p.id if p else None


def brief_domain_tokens(brief: dict[str, Any]) -> set[str]:
    blob = " ".join(
        [
            str(brief.get("domain") or ""),
            str(brief.get("mission") or ""),
            " ".join(str(c) for c in (brief.get("categories") or [])),
        ]
    ).lower()
    stop = {
        "the",
        "and",
        "for",
        "with",
        "this",
        "that",
        "from",
        "your",
        "into",
        "about",
        "help",
        "answer",
        "questions",
        "assistant",
    }
    return {
        w
        for w in re.findall(r"[a-z0-9]{3,}", blob)
        if w not in stop
    }


def document_matches_brief(
    doc: m.CorpusDocument,
    brief: dict[str, Any],
    *,
    min_score: float = 0.12,
) -> bool:
    """
    True if doc is in-scope for the active plan.
    Prefer explicit project_id match; legacy null project_id requires content fit.
    """
    brief_pid = brief.get("id") or brief.get("project_id")
    doc_pid = doc.project_id or _meta(doc).get("project_id")
    if brief_pid and doc_pid:
        return str(doc_pid) == str(brief_pid)
    if brief_pid and not doc_pid:
        # Legacy unscoped: only allow if content clearly matches this plan's domain
        return domain_relevance_score(
            f"{doc.title or ''}\n{doc.content_text or ''}", brief
        ) >= min_score
    # No active project id in brief — keep permissive for tests
    return True


def domain_relevance_score(text: str, brief: dict[str, Any]) -> float:
    """0..1 overlap of brief domain/mission/category tokens with document text."""
    tokens = brief_domain_tokens(brief)
    if not tokens:
        return 0.0
    low = (text or "").lower()
    hits = sum(1 for t in tokens if t in low)
    return hits / max(len(tokens), 1)


def list_corpus(
    db: Session,
    *,
    tenant_id: str,
    owner_user_id: str | None = None,
    project_id: str | None = None,
    brief: dict[str, Any] | None = None,
    limit: int = 50,
    scope_to_plan: bool = True,
) -> list[m.CorpusDocument]:
    """
    List active corpus docs. When scope_to_plan=True (default for mining/UI),
    only return docs for the active research project (or legacy docs that
    match the brief domain).
    """
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
    rows = q.limit(max(limit * 4, 50)).all()

    if not scope_to_plan:
        return rows[:limit]

    # Resolve project + brief for filtering
    if brief is None:
        from helix.services.brief import get_active_project, project_to_dict

        proj = get_active_project(db, tenant_id)
        brief = project_to_dict(proj) if proj else {}
        if proj and not brief.get("id"):
            brief["id"] = proj.id
    if project_id is None:
        project_id = brief.get("id") or brief.get("project_id") or active_project_id(
            db, tenant_id
        )
    if project_id and not brief.get("id"):
        brief = {**brief, "id": project_id, "project_id": project_id}

    out: list[m.CorpusDocument] = []
    for doc in rows:
        if document_matches_brief(doc, brief):
            out.append(doc)
        if len(out) >= limit:
            break
    return out


def add_paste(
    db: Session,
    *,
    tenant_id: str,
    owner_user_id: str | None,
    title: str,
    content: str,
    category: str = "general",
    project_id: str | None = None,
) -> dict[str, Any]:
    body = _clean(content)
    if len(body) < 40:
        return {"ok": False, "error": "Paste at least ~40 characters of useful content."}
    if not project_id:
        project_id = active_project_id(db, tenant_id)
    h = _hash_text(body)
    q = db.query(m.CorpusDocument).filter_by(tenant_id=tenant_id, content_hash=h)
    if project_id:
        q = q.filter(
            (m.CorpusDocument.project_id == project_id)
            | (m.CorpusDocument.project_id.is_(None))
        )
    existing = q.first()
    if existing:
        # Bind legacy unscoped doc to this plan if still null
        if project_id and not existing.project_id:
            existing.project_id = project_id
            meta = _meta(existing)
            meta["project_id"] = project_id
            existing.metadata_json = json.dumps(meta)
            existing.updated_at = _now()
            db.commit()
            db.refresh(existing)
        elif project_id and existing.project_id and existing.project_id != project_id:
            # Same text under a different plan → new row (may fail unique on old DBs)
            pass
        else:
            return {"ok": True, "duplicate": True, "document": document_to_dict(existing)}
        if existing.project_id == project_id or not project_id:
            return {"ok": True, "duplicate": True, "document": document_to_dict(existing)}

    meta = {"origin": "user_paste", "project_id": project_id}
    row = m.CorpusDocument(
        id=_uid(),
        tenant_id=tenant_id,
        project_id=project_id,
        owner_user_id=owner_user_id,
        title=(title or "Pasted document")[:500],
        url=None,
        content_text=body,
        source_kind="paste",
        content_hash=h,
        category=(category or "general")[:120],
        status="active",
        metadata_json=json.dumps(meta),
    )
    db.add(row)
    try:
        db.commit()
    except Exception:
        db.rollback()
        # Fallback if unique constraint still tenant+hash only
        existing = (
            db.query(m.CorpusDocument)
            .filter_by(tenant_id=tenant_id, content_hash=h)
            .first()
        )
        if existing:
            if project_id and not existing.project_id:
                existing.project_id = project_id
                m2 = _meta(existing)
                m2["project_id"] = project_id
                existing.metadata_json = json.dumps(m2)
                db.commit()
            return {"ok": True, "duplicate": True, "document": document_to_dict(existing)}
        raise
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
    project_id: str | None = None,
) -> dict[str, Any]:
    url = (url or "").strip()
    if not url.startswith("http://") and not url.startswith("https://"):
        return {"ok": False, "error": "URL must start with http:// or https://"}
    parsed = urlparse(url)
    if not parsed.netloc:
        return {"ok": False, "error": "Invalid URL"}
    if not project_id:
        project_id = active_project_id(db, tenant_id)

    body = ""
    page_title = title
    if fetch:
        try:
            from helix.services.gather import apify_client

            item, _meta = apify_client.fetch_page(url)
            body = _clean(item.get("text") or item.get("content") or "")
            page_title = page_title or (item.get("title") or "")
        except Exception as e:  # noqa: BLE001
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
        if project_id and not existing.project_id:
            existing.project_id = project_id
            meta = _meta(existing)
            meta["project_id"] = project_id
            existing.metadata_json = json.dumps(meta)
            db.commit()
            db.refresh(existing)
        return {"ok": True, "duplicate": True, "document": document_to_dict(existing)}

    host = parsed.netloc
    row = m.CorpusDocument(
        id=_uid(),
        tenant_id=tenant_id,
        project_id=project_id,
        owner_user_id=owner_user_id,
        title=(page_title or f"Doc from {host}")[:500],
        url=url[:1000],
        content_text=body,
        source_kind="url",
        content_hash=h,
        category=(category or "general")[:120],
        status="active",
        metadata_json=json.dumps(
            {"origin": "user_url", "host": host, "project_id": project_id}
        ),
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


def corpus_count(db: Session, tenant_id: str, project_id: str | None = None) -> int:
    q = db.query(m.CorpusDocument).filter_by(tenant_id=tenant_id, status="active")
    if project_id:
        q = q.filter(m.CorpusDocument.project_id == project_id)
    return q.count()


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
    """Hand-off: plan-scoped corpus training units → GoldExample rows."""
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
    brief_cats = [str(c) for c in (brief.get("categories") or []) if str(c).strip()]

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
        # Prefer unit-level category from content; re-infer against brief
        category = unit.get("category") or "general"
        if brief_cats:
            category = infer_category_from_text(
                f"{title}\n{evidence}", brief_cats
            )
        if len(evidence) < 40:
            rejected += 1
            details.append(
                {"source_ref": ref, "status": "rejected", "reasons": ["evidence_too_short"]}
            )
            continue
        topic = re.sub(r"[^a-z0-9]+", "_", category.lower()).strip("_") or "general"

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
                rationale=(pair.get("rationale") or "Corpus FAQ → plan gold")[:1000],
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
                    "project_id": brief.get("id") or brief.get("project_id"),
                },
            )
        except Exception as e:  # noqa: BLE001
            errors.append(f"{ref}:{e}")
            details.append({"source_ref": ref, "status": "error", "error": str(e)[:200]})
            continue

        if not g:
            if at_cap:
                skipped += 1
                details.append(
                    {
                        "source_ref": ref,
                        "status": "goal_cap_reached",
                        "verified_count": verified_toward_cap,
                        "gold_target": scope.gold_target_count,
                    }
                )
            else:
                rejected += 1
                details.append({"source_ref": ref, "status": "write_returned_null"})
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
                "topic": topic,
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
    """Split a pasted FAQ into training units (title + evidence body)."""
    text = (content or "").strip()
    if not text:
        return []
    chunks = re.split(
        r"(?=(?:^|[\n\r]|\s)(?:Q(?:uestion)?\s*\d+\s*[:.)\-]|#{1,3}\s+\S|\d{1,2}[\.)]\s+[A-Z]))",
        text,
        flags=re.I | re.M,
    )
    if len([c for c in chunks if len((c or "").strip()) >= 40]) < 2:
        chunks = re.split(r"(?=(?:Q(?:uestion)?\s*\d+\s*[:.)\-]))", text, flags=re.I)
    units: list[dict[str, str]] = []
    for raw in chunks:
        part = re.sub(r"[ \t]+", " ", (raw or "").strip())
        part = re.sub(r"\n{2,}", "\n", part).strip()
        if len(part) < 40:
            continue
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
    return units[:12]


def infer_category_from_text(text: str, brief_categories: list[str]) -> str:
    """Map unit title+evidence to the closest active plan category (sub-topic)."""
    low = (text or "").lower()
    cats = [str(c) for c in (brief_categories or []) if str(c).strip()]
    if not cats:
        return "general"
    best = cats[0]
    best_score = -1.0
    for c in cats:
        cl = c.lower()
        tokens = re.findall(r"[a-z0-9]{3,}", cl)
        score = float(sum(2 if t in low else 0 for t in tokens))
        # Phrase match on full category name
        if cl in low:
            score += 5
        # Synonym boosts — support
        if "late" in cl and any(x in low for x in ("late", "delay", "eta", "arrived late")):
            score += 3
        if "missing" in cl and "missing" in low:
            score += 3
        if "wrong" in cl and any(x in low for x in ("wrong", "incorrect")):
            score += 3
        if "refund" in cl and "refund" in low:
            score += 3
        if "driver" in cl and "driver" in low:
            score += 3
        if "account" in cl and "account" in low:
            score += 2
        if "damag" in low and "damag" in cl:
            score += 3
        # HR
        if any(x in cl for x in ("pto", "leave", "accrual", "carry")) and any(
            x in low for x in ("pto", "paid time", "accrue", "carry over", "vacation", "leave")
        ):
            score += 4
        if any(x in cl for x in ("remote", "hybrid")) and any(
            x in low for x in ("remote", "hybrid", "work from home", "wfh")
        ):
            score += 4
        if "benefit" in cl and any(x in low for x in ("benefit", "enrollment", "insurance")):
            score += 3
        if "onboard" in cl and any(x in low for x in ("onboard", "new hire", "start date")):
            score += 3
        # Penalize remote category when text is clearly PTO-only
        if any(x in cl for x in ("remote", "hybrid")) and any(
            x in low for x in ("pto", "accrue", "carry over")
        ) and not any(x in low for x in ("remote", "hybrid", "wfh", "work from home")):
            score -= 5
        # Penalize PTO category when text is clearly remote-only
        if any(x in cl for x in ("pto", "leave", "accrual")) and any(
            x in low for x in ("remote", "hybrid", "wfh")
        ) and not any(x in low for x in ("pto", "accrue", "vacation", "leave", "carry")):
            score -= 3
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
    Turn *plan-scoped* corpus docs into DiscoveryCandidate + EvidenceStaging.
    Never promotes another plan's corpus into this plan's evidence pool.
    """
    brief = dict(brief or {})
    from helix.services.brief import get_active_project, project_to_dict

    proj = get_active_project(db, tenant_id)
    if proj:
        if not brief.get("domain"):
            brief = {**project_to_dict(proj), **brief}
        brief["id"] = proj.id
        brief["project_id"] = proj.id

    cats = [str(c) for c in (brief.get("categories") or []) if str(c).strip()]
    docs = list_corpus(
        db,
        tenant_id=tenant_id,
        owner_user_id=owner_user_id,
        project_id=brief.get("id") or brief.get("project_id"),
        brief=brief,
        limit=max(batch_size * 3, 10),
        scope_to_plan=True,
    )
    candidates_created = 0
    evidence_written = 0
    units_out: list[dict[str, Any]] = []
    skipped_other_plan = 0

    for doc in docs:
        if not document_matches_brief(doc, brief):
            skipped_other_plan += 1
            continue
        units = extract_training_units(
            title=doc.title or "Corpus document",
            content=doc.content_text or "",
            category=doc.category or "general",
        )
        for i, unit in enumerate(units):
            cat = (
                infer_category_from_text(f"{unit['title']}\n{unit['evidence']}", cats)
                if cats
                else (unit.get("category") or "general")
            )
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
                    "project_id": brief.get("id") or brief.get("project_id"),
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

            cand_row = (
                db.query(m.DiscoveryCandidate)
                .filter_by(id=cand_id, tenant_id=tenant_id)
                .first()
            )
            if cand_row:
                cand_row.status = "staged"
                cand_row.category = cat
                cand_row.title = unit["title"][:500]

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
                            "User-supplied corpus for active research plan "
                            f"(project_id={brief.get('id')}). Full text staged."
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
                # Keep brand aligned to current plan domain for filtering
                if brief.get("domain"):
                    camp.brand = (brief.get("domain") or "")[:200]
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
                    "project_id": brief.get("id") or brief.get("project_id"),
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
        "skipped_other_plan": skipped_other_plan,
        "project_id": brief.get("id") or brief.get("project_id"),
    }
