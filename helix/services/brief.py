"""Research brief helpers shared by runner, tools, and API."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from helix.db import models as m


def project_to_dict(p: m.ResearchProject) -> dict[str, Any]:
    return {
        "id": p.id,
        "slug": p.slug,
        "name": p.name,
        "domain": p.domain,
        "mission": p.mission,
        "research_questions": json.loads(p.research_questions_json or "[]"),
        "sources": json.loads(p.sources_json or "[]"),
        "categories": json.loads(p.categories_json or "[]"),
        "phase_targets": json.loads(p.phase_targets_json or "{}"),
        "success_metrics": json.loads(p.success_metrics_json or "[]"),
        "topic_keys": json.loads(p.topic_keys_json or "[]"),
        "agent_instructions": p.agent_instructions,
        "output_notes": p.output_notes,
        "is_active": p.is_active,
        "updated_at": p.updated_at.isoformat() if p.updated_at else None,
    }


def get_active_project(db: Session, tenant_id: str) -> m.ResearchProject | None:
    return (
        db.query(m.ResearchProject)
        .filter_by(tenant_id=tenant_id, is_active=True)
        .order_by(m.ResearchProject.updated_at.desc())
        .first()
    )


def sync_workspace_from_brief(
    db: Session,
    tenant_id: str,
    *,
    force_queue: bool = False,
) -> dict:
    """Align CategoryState + discovery work queue with the active Research Brief.

    Prevents the pipeline from staying stuck on bootstrap influencer categories
    after the user sets a custom domain/plan.
    """
    import uuid
    from datetime import datetime, timezone

    def _uid(prefix: str = "") -> str:
        return f"{prefix}{uuid.uuid4().hex[:12]}"

    project = get_active_project(db, tenant_id)
    if not project:
        return {"ok": False, "error": "no_brief", "categories": 0, "queue": 0}

    d = project_to_dict(project)
    categories = [str(c).strip() for c in (d.get("categories") or []) if str(c).strip()]
    sources = [str(s).strip() for s in (d.get("sources") or []) if str(s).strip()]
    targets = d.get("phase_targets") or {}
    if not categories:
        categories = ["general"]
    if not sources:
        sources = ["web", "blog", "docs"]

    # Upsert categories from brief
    existing = {
        c.name: c
        for c in db.query(m.CategoryState).filter_by(tenant_id=tenant_id).all()
    }
    brief_set = set(categories)
    for name in categories:
        tgt = 40
        if isinstance(targets, dict) and name in targets:
            try:
                tgt = int(targets[name])
            except (TypeError, ValueError):
                tgt = 40
        if name in existing:
            existing[name].phase_target = tgt
        else:
            db.add(
                m.CategoryState(
                    id=_uid("cat_"),
                    tenant_id=tenant_id,
                    name=name,
                    phase_target=tgt,
                )
            )

    # Soft-deprecate categories not in brief (keep history but zero target + clear miss counters)
    for name, row in existing.items():
        if name not in brief_set:
            row.phase_target = 0
            row.weeks_missed_target = 0

    open_q = (
        db.query(m.WorkQueueItem)
        .filter_by(tenant_id=tenant_id, status="open", assigned_agent="discovery")
        .count()
    )
    created_q = 0
    if force_queue or open_q == 0:
        # supersede old open discovery items so new domain takes over
        for item in (
            db.query(m.WorkQueueItem)
            .filter_by(tenant_id=tenant_id, status="open", assigned_agent="discovery")
            .all()
        ):
            item.status = "superseded"
            item.updated_at = datetime.now(timezone.utc)
        # create queue: each category × top sources
        src_use = sources[:4]
        for i, cat in enumerate(categories[:8]):
            for j, src in enumerate(src_use):
                db.add(
                    m.WorkQueueItem(
                        id=_uid("wq_"),
                        tenant_id=tenant_id,
                        category=cat,
                        source=src,
                        priority_score=round(1.0 - 0.05 * i - 0.02 * j, 3),
                        assigned_agent="discovery",
                        status="open",
                    )
                )
                created_q += 1

    # Ontology must follow the active plan (not bootstrap Brand/Creator/Campaign)
    from helix.services.domain_ontology import sync_ontology_from_brief

    db.flush()
    ont = sync_ontology_from_brief(db, tenant_id)

    db.commit()
    return {
        "ok": True,
        "categories": len(categories),
        "queue": created_q,
        "domain": d.get("domain"),
        "ontology_types": ont.get("types", 0),
        "ontology": ont.get("type_names") or [],
    }


def build_domain_context(db: Session, tenant_id: str) -> str:
    """Text block injected into every agent system prompt."""
    project = get_active_project(db, tenant_id)
    if not project:
        return (
            "\n\n=== ACTIVE RESEARCH BRIEF ===\n"
            "No active research brief configured. Operate carefully; ask for a brief.\n"
            "=== END RESEARCH BRIEF ===\n"
        )
    d = project_to_dict(project)
    schemas = (
        db.query(m.TopicSchema)
        .filter_by(tenant_id=tenant_id, is_active=True)
        .all()
    )
    schema_lines = []
    for s in schemas:
        schema_lines.append(
            f"- {s.topic}"
            + (f" ({s.display_name})" if s.display_name else "")
            + (f": {s.description}" if s.description else "")
        )
    text = (
        "\n\n=== ACTIVE RESEARCH BRIEF ===\n"
        f"Project: {d['name']} ({d['slug']})\n"
        f"Domain: {d['domain']}\n"
        f"Mission: {d['mission']}\n"
        f"Research questions: {json.dumps(d['research_questions'], ensure_ascii=False)}\n"
        f"Categories: {json.dumps(d['categories'])}\n"
        f"Sources: {json.dumps(d['sources'])}\n"
        f"Phase targets: {json.dumps(d['phase_targets'])}\n"
        f"Success metrics: {json.dumps(d['success_metrics'], ensure_ascii=False)}\n"
        f"Gold topic keys: {json.dumps(d['topic_keys'])}\n"
        f"Topic schemas online:\n"
        + ("\n".join(schema_lines) if schema_lines else "(none)")
        + "\n"
        f"Agent instructions: {d['agent_instructions'] or '(none)'}\n"
        f"Output notes: {d['output_notes'] or '(none)'}\n"
    )
    ont_rows = db.query(m.OntologyType).filter_by(tenant_id=tenant_id).all()
    if ont_rows:
        ont_lines = [
            f"- {o.type_name} ({o.kind}): {o.description or ''}" for o in ont_rows[:40]
        ]
        text += (
            "Domain ontology (use these types only for extraction):\n"
            + "\n".join(ont_lines)
            + "\n"
        )
    text += "=== END RESEARCH BRIEF ===\n"
    return text


def schema_to_dict(s: m.TopicSchema) -> dict[str, Any]:
    sample = None
    if s.sample_row_json:
        try:
            sample = json.loads(s.sample_row_json)
        except json.JSONDecodeError:
            sample = s.sample_row_json
    try:
        schema = json.loads(s.schema_json)
    except json.JSONDecodeError:
        schema = {"raw": s.schema_json}
    return {
        "id": s.id,
        "topic": s.topic,
        "display_name": s.display_name or s.topic,
        "description": s.description,
        "schema": schema,
        "sample_row": sample,
        "export_format": s.export_format or "jsonl",
        "is_active": bool(s.is_active) if s.is_active is not None else True,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }
