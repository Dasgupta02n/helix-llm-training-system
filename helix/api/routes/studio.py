"""Research brief, schema studio, and dataset export APIs."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from helix.api.deps import get_current_user
from helix.db import models as m
from helix.db.session import get_db
from helix.services.brief import get_active_project, project_to_dict, schema_to_dict

router = APIRouter(prefix="/api/t/{slug}", tags=["studio"])


def _uid(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _tenant_for(user: m.User, slug: str, db: Session) -> m.Tenant:
    tenant = db.query(m.Tenant).filter_by(slug=slug, is_active=True).first()
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    if not user.is_superadmin:
        mem = (
            db.query(m.Membership)
            .filter_by(user_id=user.id, tenant_id=tenant.id)
            .first()
        )
        if not mem:
            raise HTTPException(403, "Forbidden")
    return tenant


# ── Research projects / briefs ───────────────────────────────────────


class ProjectIn(BaseModel):
    slug: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9-]+$")
    name: str
    domain: str = ""
    mission: str = ""
    research_questions: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    phase_targets: dict[str, int] = Field(default_factory=dict)
    success_metrics: list[Any] = Field(default_factory=list)
    topic_keys: list[str] = Field(default_factory=list)
    agent_instructions: str | None = None
    output_notes: str | None = None
    is_active: bool = True


class ProjectUpdate(BaseModel):
    name: str | None = None
    domain: str | None = None
    mission: str | None = None
    research_questions: list[str] | None = None
    sources: list[str] | None = None
    categories: list[str] | None = None
    phase_targets: dict[str, int] | None = None
    success_metrics: list[Any] | None = None
    topic_keys: list[str] | None = None
    agent_instructions: str | None = None
    output_notes: str | None = None
    is_active: bool | None = None


@router.get("/projects")
def list_projects(
    slug: str,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    tenant = _tenant_for(user, slug, db)
    rows = (
        db.query(m.ResearchProject)
        .filter_by(tenant_id=tenant.id)
        .order_by(m.ResearchProject.updated_at.desc())
        .all()
    )
    return [project_to_dict(r) for r in rows]


@router.get("/projects/active")
def active_project(
    slug: str,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    tenant = _tenant_for(user, slug, db)
    p = get_active_project(db, tenant.id)
    if not p:
        return {"brief": None}
    return {"brief": project_to_dict(p)}


@router.post("/projects")
def create_project(
    slug: str,
    body: ProjectIn,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    tenant = _tenant_for(user, slug, db)
    if db.query(m.ResearchProject).filter_by(tenant_id=tenant.id, slug=body.slug).first():
        raise HTTPException(400, "Project slug already exists")
    if body.is_active:
        for r in db.query(m.ResearchProject).filter_by(tenant_id=tenant.id, is_active=True):
            r.is_active = False
    p = m.ResearchProject(
        id=_uid("prj_"),
        tenant_id=tenant.id,
        slug=body.slug,
        name=body.name,
        domain=body.domain,
        mission=body.mission,
        research_questions_json=json.dumps(body.research_questions),
        sources_json=json.dumps(body.sources),
        categories_json=json.dumps(body.categories),
        phase_targets_json=json.dumps(body.phase_targets),
        success_metrics_json=json.dumps(body.success_metrics),
        topic_keys_json=json.dumps(body.topic_keys),
        agent_instructions=body.agent_instructions,
        output_notes=body.output_notes,
        is_active=body.is_active,
        updated_at=_now(),
    )
    db.add(p)
    # Sync category phase targets when provided
    _sync_phase_targets(db, tenant.id, body.phase_targets, body.categories)
    db.commit()
    db.refresh(p)
    return project_to_dict(p)


@router.put("/projects/{project_slug}")
def update_project(
    slug: str,
    project_slug: str,
    body: ProjectUpdate,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    tenant = _tenant_for(user, slug, db)
    p = (
        db.query(m.ResearchProject)
        .filter_by(tenant_id=tenant.id, slug=project_slug)
        .first()
    )
    if not p:
        raise HTTPException(404, "Project not found")
    data = body.model_dump(exclude_unset=True)
    if data.get("is_active") is True:
        for r in db.query(m.ResearchProject).filter_by(tenant_id=tenant.id, is_active=True):
            if r.id != p.id:
                r.is_active = False
    mapping = {
        "research_questions": "research_questions_json",
        "sources": "sources_json",
        "categories": "categories_json",
        "phase_targets": "phase_targets_json",
        "success_metrics": "success_metrics_json",
        "topic_keys": "topic_keys_json",
    }
    for k, v in data.items():
        if k in mapping:
            setattr(p, mapping[k], json.dumps(v))
        elif hasattr(p, k):
            setattr(p, k, v)
    p.updated_at = _now()
    if "phase_targets" in data or "categories" in data:
        targets = data.get("phase_targets")
        if targets is None:
            targets = json.loads(p.phase_targets_json or "{}")
        cats = data.get("categories")
        if cats is None:
            cats = json.loads(p.categories_json or "[]")
        _sync_phase_targets(db, tenant.id, targets, cats)
    db.commit()
    db.refresh(p)
    return project_to_dict(p)


@router.post("/projects/{project_slug}/activate")
def activate_project(
    slug: str,
    project_slug: str,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    tenant = _tenant_for(user, slug, db)
    p = (
        db.query(m.ResearchProject)
        .filter_by(tenant_id=tenant.id, slug=project_slug)
        .first()
    )
    if not p:
        raise HTTPException(404, "Project not found")
    for r in db.query(m.ResearchProject).filter_by(tenant_id=tenant.id):
        r.is_active = r.id == p.id
        r.updated_at = _now()
    db.commit()
    return project_to_dict(p)


def _sync_phase_targets(
    db: Session,
    tenant_id: str,
    targets: dict[str, int],
    categories: list[str],
) -> None:
    names = set(categories or []) | set((targets or {}).keys())
    for name in names:
        if not name:
            continue
        row = (
            db.query(m.CategoryState)
            .filter_by(tenant_id=tenant_id, name=name)
            .first()
        )
        target = int((targets or {}).get(name, 50))
        if row:
            row.phase_target = target
        else:
            db.add(
                m.CategoryState(
                    id=_uid("cat_"),
                    tenant_id=tenant_id,
                    name=name,
                    phase_target=target,
                    verified_count=0,
                )
            )


# ── Topic schema studio ──────────────────────────────────────────────


class SchemaIn(BaseModel):
    topic: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9_\-]+$")
    display_name: str = ""
    description: str = ""
    json_schema: dict[str, Any] = Field(default_factory=dict)
    sample_row: dict[str, Any] | None = None
    export_format: str = "jsonl"
    is_active: bool = True


class SchemaUpdate(BaseModel):
    display_name: str | None = None
    description: str | None = None
    json_schema: dict[str, Any] | None = None
    sample_row: dict[str, Any] | None = None
    export_format: str | None = None
    is_active: bool | None = None


@router.get("/schemas")
def list_schemas(
    slug: str,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    tenant = _tenant_for(user, slug, db)
    rows = (
        db.query(m.TopicSchema)
        .filter_by(tenant_id=tenant.id)
        .order_by(m.TopicSchema.topic)
        .all()
    )
    return [schema_to_dict(r) for r in rows]


@router.post("/schemas")
def create_schema(
    slug: str,
    body: SchemaIn,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    tenant = _tenant_for(user, slug, db)
    if db.query(m.TopicSchema).filter_by(tenant_id=tenant.id, topic=body.topic).first():
        raise HTTPException(400, "Topic already exists")
    row = m.TopicSchema(
        id=_uid("ts_"),
        tenant_id=tenant.id,
        topic=body.topic,
        display_name=body.display_name or body.topic,
        description=body.description,
        schema_json=json.dumps(body.json_schema or {}),
        sample_row_json=json.dumps(body.sample_row) if body.sample_row is not None else None,
        export_format=body.export_format or "jsonl",
        is_active=body.is_active,
        updated_at=_now(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return schema_to_dict(row)


@router.put("/schemas/{topic}")
def update_schema(
    slug: str,
    topic: str,
    body: SchemaUpdate,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    tenant = _tenant_for(user, slug, db)
    row = (
        db.query(m.TopicSchema)
        .filter_by(tenant_id=tenant.id, topic=topic)
        .first()
    )
    if not row:
        raise HTTPException(404, "Schema not found")
    data = body.model_dump(exclude_unset=True)
    if "json_schema" in data and data["json_schema"] is not None:
        row.schema_json = json.dumps(data["json_schema"])
    if "sample_row" in data:
        row.sample_row_json = (
            json.dumps(data["sample_row"]) if data["sample_row"] is not None else None
        )
    for k in ("display_name", "description", "export_format", "is_active"):
        if k in data and data[k] is not None:
            setattr(row, k, data[k])
    row.updated_at = _now()
    db.commit()
    db.refresh(row)
    return schema_to_dict(row)


@router.delete("/schemas/{topic}")
def delete_schema(
    slug: str,
    topic: str,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    tenant = _tenant_for(user, slug, db)
    row = (
        db.query(m.TopicSchema)
        .filter_by(tenant_id=tenant.id, topic=topic)
        .first()
    )
    if not row:
        raise HTTPException(404, "Schema not found")
    row.is_active = False
    row.updated_at = _now()
    db.commit()
    return {"ok": True, "topic": topic, "is_active": False}


# ── Dataset versions + export ────────────────────────────────────────


@router.get("/datasets")
def list_datasets(
    slug: str,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    tenant = _tenant_for(user, slug, db)
    rows = (
        db.query(m.DatasetVersion)
        .filter_by(tenant_id=tenant.id)
        .order_by(m.DatasetVersion.created_at.desc())
        .all()
    )
    out = []
    for r in rows:
        try:
            manifest = json.loads(r.manifest_json or "{}")
        except json.JSONDecodeError:
            manifest = {}
        count = (
            db.query(m.TrainingExample)
            .filter_by(tenant_id=tenant.id, dataset_version=r.version)
            .count()
        )
        out.append(
            {
                "id": r.id,
                "version": r.version,
                "manifest": manifest,
                "example_count": count,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
        )
    return out


@router.get("/datasets/{version}/export")
def export_dataset(
    slug: str,
    version: str,
    split: str | None = None,
    format: str = "jsonl",
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    tenant = _tenant_for(user, slug, db)
    q = db.query(m.TrainingExample).filter_by(
        tenant_id=tenant.id,
        dataset_version=version,
        reserved_for_benchmark=False,
    )
    if split:
        q = q.filter_by(split=split)
    rows = q.order_by(m.TrainingExample.topic, m.TrainingExample.id).all()

    # Fallback: if no rows tagged with version, export approved non-benchmark pool
    # under a synthetic view (useful before curator runs)
    if not rows and version == "approved-pool":
        rows = (
            db.query(m.TrainingExample)
            .filter_by(
                tenant_id=tenant.id,
                review_status="approved",
                reserved_for_benchmark=False,
            )
            .all()
        )

    if format not in {"jsonl", "json"}:
        raise HTTPException(400, "format must be jsonl or json")

    def row_obj(r: m.TrainingExample) -> dict:
        return {
            "id": r.id,
            "topic": r.topic,
            "input": r.input_text,
            "output": r.output_text,
            "rationale": r.rationale,
            "difficulty": r.difficulty,
            "is_negative": r.is_negative,
            "split": r.split,
            "seed_id": r.seed_id,
            "dataset_version": r.dataset_version or version,
        }

    safe_ver = re.sub(r"[^a-zA-Z0-9_.-]+", "_", version)
    if format == "json":
        payload = json.dumps([row_obj(r) for r in rows], ensure_ascii=False, indent=2)
        return PlainTextResponse(
            payload,
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="helix_{slug}_{safe_ver}.json"'
            },
        )

    def gen():
        for r in rows:
            yield json.dumps(row_obj(r), ensure_ascii=False) + "\n"

    return StreamingResponse(
        gen(),
        media_type="application/x-ndjson",
        headers={
            "Content-Disposition": f'attachment; filename="helix_{slug}_{safe_ver}.jsonl"'
        },
    )


@router.post("/datasets/snapshot")
def snapshot_approved_pool(
    slug: str,
    version: str = "manual_snapshot",
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    """Create a dataset version from currently approved non-benchmark examples."""
    tenant = _tenant_for(user, slug, db)
    if not re.match(r"^[a-zA-Z0-9_.-]+$", version):
        raise HTTPException(400, "Invalid version name")
    if (
        db.query(m.DatasetVersion)
        .filter_by(tenant_id=tenant.id, version=version)
        .first()
    ):
        raise HTTPException(400, "Version already exists")

    rows = (
        db.query(m.TrainingExample)
        .filter_by(
            tenant_id=tenant.id,
            review_status="approved",
            reserved_for_benchmark=False,
        )
        .all()
    )
    by_topic: dict[str, int] = {}
    by_split: dict[str, int] = {}
    for r in rows:
        r.dataset_version = version
        by_topic[r.topic] = by_topic.get(r.topic, 0) + 1
        sp = r.split or "unassigned"
        by_split[sp] = by_split.get(sp, 0) + 1

    manifest = {
        "version": version,
        "count": len(rows),
        "by_topic": by_topic,
        "by_split": by_split,
        "created_by": user.email,
        "source": "approved_pool_snapshot",
    }
    did = _uid("ds_")
    db.add(
        m.DatasetVersion(
            id=did,
            tenant_id=tenant.id,
            version=version,
            manifest_json=json.dumps(manifest),
        )
    )
    db.commit()
    return {"ok": True, "id": did, "version": version, "count": len(rows), "manifest": manifest}
