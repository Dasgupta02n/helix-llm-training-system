from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from helix.agents.catalog import PIPELINE_ORDER, get_agent, list_agents
from helix.agents.runner import run_agent
from helix.api.deps import get_current_user
from helix.api.schemas import AgentRunRequest, EscalationDecision, PipelineRunRequest
from helix.db import models as m
from helix.db.session import get_db
from helix.tools.handlers import ToolContext, get_success_metrics, get_agent_health, get_unified_escalation_queue, route_human_decision

router = APIRouter(prefix="/api/t/{slug}", tags=["agents"])


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


@router.get("/agents")
def agents_list() -> list[dict]:
    return [
        {
            "key": a.key,
            "name": a.name,
            "role": a.role,
            "reports_to": a.reports_to,
            "budget_tier": a.budget_tier,
            "goal": a.goal,
            "tools": list(a.tools),
        }
        for a in list_agents()
    ]


@router.get("/agents/{agent_key}")
def agent_detail(agent_key: str) -> dict:
    try:
        a = get_agent(agent_key)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    return {
        "key": a.key,
        "name": a.name,
        "role": a.role,
        "reports_to": a.reports_to,
        "budget_tier": a.budget_tier,
        "goal": a.goal,
        "tools": list(a.tools),
        "system_prompt": a.system_prompt,
    }


@router.post("/agents/{agent_key}/run")
def agent_run(
    slug: str,
    agent_key: str,
    body: AgentRunRequest,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    tenant = _tenant_for(user, slug, db)
    try:
        get_agent(agent_key)
    except KeyError as e:
        raise HTTPException(404, str(e)) from e
    try:
        return run_agent(
            db,
            tenant.id,
            agent_key,
            message=body.message,
            trigger="api",
            owner_user_id=user.id,
        )
    except RuntimeError as e:
        raise HTTPException(503, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"Agent run failed: {e}") from e


@router.post("/pipeline/run")
def pipeline_run(
    slug: str,
    body: PipelineRunRequest,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    tenant = _tenant_for(user, slug, db)
    order = body.agents or PIPELINE_ORDER
    results = []
    for key in order:
        try:
            get_agent(key)
            result = run_agent(
                db,
                tenant.id,
                key,
                message=body.message,
                trigger="pipeline",
                owner_user_id=user.id,
            )
            results.append(result)
        except Exception as e:  # noqa: BLE001
            results.append({"agent": key, "status": "error", "error": str(e)})
            break
    return {"tenant": slug, "results": results}


@router.get("/dashboard")
def dashboard(
    slug: str,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    tenant = _tenant_for(user, slug, db)
    ctx = ToolContext(db, tenant.id, "operations_dashboard")
    return {
        "tenant": {
            "id": tenant.id,
            "slug": tenant.slug,
            "name": tenant.name,
            "plan": tenant.plan,
            "spent_usd": tenant.spent_usd,
            "monthly_budget_usd": tenant.monthly_budget_usd,
        },
        "metrics": get_success_metrics(ctx),
        "agent_health": get_agent_health(ctx),
        "escalations": get_unified_escalation_queue(ctx),
    }


@router.post("/escalations/{escalation_id}/resolve")
def resolve_escalation(
    slug: str,
    escalation_id: str,
    body: EscalationDecision,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    tenant = _tenant_for(user, slug, db)
    ctx = ToolContext(db, tenant.id, "operations_dashboard")
    result = route_human_decision(ctx, escalation_id=escalation_id, decision=body.decision)
    db.commit()
    if not result.get("ok"):
        raise HTTPException(404, result.get("error", "not found"))
    return result


@router.get("/runs")
def list_runs(
    slug: str,
    limit: int = 50,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    tenant = _tenant_for(user, slug, db)
    rows = (
        db.query(m.AgentRun)
        .filter_by(tenant_id=tenant.id)
        .order_by(m.AgentRun.created_at.desc())
        .limit(min(limit, 200))
        .all()
    )
    return [
        {
            "id": r.id,
            "agent": r.agent,
            "status": r.status,
            "trigger": r.trigger,
            "provider": r.provider,
            "model": r.model,
            "cost_usd": r.cost_usd,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "error": r.error,
            "output_preview": (r.output_text or "")[:240],
        }
        for r in rows
    ]


@router.get("/runs/{run_id}")
def get_run(
    slug: str,
    run_id: str,
    user: m.User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    tenant = _tenant_for(user, slug, db)
    r = db.query(m.AgentRun).filter_by(id=run_id, tenant_id=tenant.id).first()
    if not r:
        raise HTTPException(404, "Run not found")
    return {
        "id": r.id,
        "agent": r.agent,
        "status": r.status,
        "trigger": r.trigger,
        "input_message": r.input_message,
        "output_text": r.output_text,
        "tool_trace_json": r.tool_trace_json,
        "provider": r.provider,
        "model": r.model,
        "cost_usd": r.cost_usd,
        "error": r.error,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
    }
