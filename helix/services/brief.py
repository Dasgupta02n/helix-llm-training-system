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
    return (
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
        "=== END RESEARCH BRIEF ===\n"
    )


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
