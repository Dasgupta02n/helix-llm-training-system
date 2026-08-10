"""Agent execution loop with OpenRouter/xAI tool calling."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from helix.agents.catalog import get_agent
from helix.config import get_settings
from helix.db import models as m
from helix.llm.client import (
    estimate_cost_usd,
    get_llm_client_for_tenant,
    parse_tool_args,
    serialize_tool_calls,
)
from helix.services.brief import build_domain_context
from helix.tools.handlers import ToolContext
from helix.tools.registry import execute_tool, tools_for_agent


def _uid() -> str:
    import uuid

    return f"run_{uuid.uuid4().hex[:12]}"


def run_agent(
    db: Session,
    tenant_id: str,
    agent_key: str,
    message: str | None = None,
    trigger: str = "manual",
    owner_user_id: str | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    agent = get_agent(agent_key)
    tenant = db.query(m.Tenant).filter_by(id=tenant_id).first()
    if not tenant or not tenant.is_active:
        raise ValueError("Tenant not found or inactive")
    if tenant.spent_usd >= tenant.monthly_budget_usd:
        raise ValueError("Tenant monthly LLM budget exceeded")

    client = get_llm_client_for_tenant(tenant)
    tools = tools_for_agent(agent_key)
    run_id = _uid()
    run = m.AgentRun(
        id=run_id,
        tenant_id=tenant_id,
        agent=agent_key,
        status="running",
        trigger=trigger,
        input_message=message,
        provider=client.provider,
        model=client.model,
    )
    db.add(run)
    db.commit()

    domain_ctx = build_domain_context(db, tenant_id)
    ownership_note = ""
    if owner_user_id:
        ownership_note = (
            "\n\nUSER ACCOUNT STORAGE: All approved gold examples and synthesized "
            "variations must be attributable to this user account and retained indefinitely. "
            f"Acting owner_user_id={owner_user_id}.\n"
        )
    architecture_note = (
        "\n\n=== SYSTEM ARCHITECTURE (mandatory) ===\n"
        "GATHER = Apify only (tools: trigger_discovery, collect_full_evidence).\n"
        "JUDGE = OpenRouter/you only (verify, extract quality, adversarial, strategy).\n"
        "Never invent scrape results, posts, captions, or URLs.\n"
        "If gather tools error or return empty needs_judgment, stop — do not fabricate.\n"
        "Only pass items that already need_judgment / are staged in the DB.\n"
        "=== END ARCHITECTURE ===\n"
    )
    system_prompt = agent.system_prompt + domain_ctx + architecture_note + ownership_note

    user_msg = message or (
        f"Run your standard operating cycle as {agent.name}. "
        f"Use your tools. Goal: {agent.goal}. "
        f"Honor the active Research Brief injected in your system context."
    )
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_msg}]
    tool_trace: list[dict[str, Any]] = []
    ctx = ToolContext(db, tenant_id, agent_key, owner_user_id=owner_user_id)
    total_cost = 0.0
    final_text = ""

    try:
        for _round in range(settings.max_tool_rounds):
            response = client.chat(
                system=system_prompt,
                messages=messages,
                tools=tools,
                tool_choice="auto",
            )
            choice = response.choices[0]
            msg = choice.message
            usage = getattr(response, "usage", None)
            if usage:
                total_cost += estimate_cost_usd(
                    getattr(usage, "prompt_tokens", 0) or 0,
                    getattr(usage, "completion_tokens", 0) or 0,
                )

            if msg.tool_calls:
                messages.append(
                    {
                        "role": "assistant",
                        "content": msg.content or "",
                        "tool_calls": serialize_tool_calls(msg),
                    }
                )
                for tc in msg.tool_calls:
                    name = tc.function.name
                    args = parse_tool_args(tc.function.arguments)
                    result = execute_tool(
                        db, tenant_id, agent_key, name, args, ctx=ctx
                    )
                    tool_trace.append({"tool": name, "arguments": args, "result": result})
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": json.dumps(result, default=str),
                        }
                    )
                db.commit()
                continue

            final_text = msg.content or ""
            break
        else:
            final_text = final_text or "Stopped after max tool rounds."

        # health
        health = (
            db.query(m.AgentHealth)
            .filter_by(tenant_id=tenant_id, agent=agent_key)
            .first()
        )
        if health:
            health.last_run_at = datetime.now(timezone.utc)
            health.last_status = "ok"
            health.run_count += 1
            health.estimated_cost_usd += total_cost
        tenant.spent_usd += total_cost

        run.status = "completed"
        run.output_text = final_text
        run.tool_trace_json = json.dumps(tool_trace, default=str)
        run.cost_usd = total_cost
        run.finished_at = datetime.now(timezone.utc)
        db.commit()

        return {
            "run_id": run_id,
            "agent": agent_key,
            "status": "completed",
            "output": final_text,
            "tool_calls": tool_trace,
            "cost_usd": total_cost,
            "provider": client.provider,
            "model": client.model,
        }
    except Exception as e:  # noqa: BLE001
        db.rollback()
        # re-attach
        run = db.query(m.AgentRun).filter_by(id=run_id).first()
        if run:
            run.status = "error"
            run.error = str(e)
            run.tool_trace_json = json.dumps(tool_trace, default=str)
            run.cost_usd = total_cost
            run.finished_at = datetime.now(timezone.utc)
        health = (
            db.query(m.AgentHealth)
            .filter_by(tenant_id=tenant_id, agent=agent_key)
            .first()
        )
        if health:
            health.last_run_at = datetime.now(timezone.utc)
            health.last_status = "error"
            health.error_count += 1
            health.run_count += 1
        db.commit()
        raise
