"""Tool schema lookup and execution."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy.orm import Session

from helix.agents.catalog import get_agent
from helix.tools.handlers import HANDLERS, ToolContext
from helix.tools.schemas import TOOL_SCHEMAS


def tools_for_agent(agent_key: str) -> list[dict[str, Any]]:
    agent = get_agent(agent_key)
    missing = [t for t in agent.tools if t not in TOOL_SCHEMAS]
    if missing:
        raise KeyError(f"Agent {agent_key} references unknown tools: {missing}")
    return [TOOL_SCHEMAS[t] for t in agent.tools]


def execute_tool(
    db: Session,
    tenant_id: str,
    agent_key: str,
    name: str,
    arguments: dict[str, Any] | str,
    ctx: ToolContext | None = None,
) -> dict[str, Any]:
    if name not in HANDLERS:
        return {"ok": False, "error": f"Unknown tool: {name}"}
    agent = get_agent(agent_key)
    if name not in agent.tools:
        return {"ok": False, "error": f"Tool '{name}' not allowed for agent '{agent_key}'"}
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            arguments = {"_raw": arguments}
    context = ctx or ToolContext(db, tenant_id, agent_key)
    try:
        result = HANDLERS[name](context, **(arguments or {}))
        return result if isinstance(result, dict) else {"result": result}
    except TypeError as e:
        return {"ok": False, "error": f"Bad arguments for {name}: {e}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
