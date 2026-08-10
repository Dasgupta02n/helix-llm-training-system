"""Helix CLI for local ops and smoke checks."""

from __future__ import annotations

import json
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table

from helix.agents.catalog import list_agents
from helix.config import get_settings
from helix.db.session import SessionLocal, init_db
from helix.db import models as m

app = typer.Typer(help="Helix multi-tenant agent platform")
console = Console()


@app.command()
def init() -> None:
    """Initialize database and bootstrap admin + demo tenant."""
    init_db()
    console.print("[green]Database initialized.[/green]")
    s = get_settings()
    console.print(f"Admin: {s.bootstrap_admin_email}")
    console.print(f"LLM provider: {s.llm_provider} / model: {s.llm_model}")


@app.command("list-agents")
def list_agents_cmd() -> None:
    table = Table(title="Helix Agents")
    table.add_column("Key")
    table.add_column("Name")
    table.add_column("Role")
    table.add_column("Reports to")
    table.add_column("Tools")
    for a in list_agents():
        table.add_row(a.key, a.name, a.role, a.reports_to or "—", str(len(a.tools)))
    console.print(table)


@app.command()
def run(
    agent: str = typer.Argument(..., help="Agent key"),
    tenant: str = typer.Option("demo", help="Tenant slug"),
    message: Optional[str] = typer.Option(None, help="Optional user message"),
) -> None:
    """Run a single agent for a tenant (uses OpenRouter/xAI)."""
    init_db()
    from helix.agents.runner import run_agent

    db = SessionLocal()
    try:
        t = db.query(m.Tenant).filter_by(slug=tenant).first()
        if not t:
            console.print(f"[red]Tenant '{tenant}' not found[/red]")
            raise typer.Exit(1)
        result = run_agent(db, t.id, agent, message=message, trigger="cli")
        console.print_json(json.dumps({
            "run_id": result["run_id"],
            "status": result["status"],
            "cost_usd": result["cost_usd"],
            "provider": result["provider"],
            "model": result["model"],
            "output": result["output"],
            "tool_calls": len(result["tool_calls"]),
        }))
    finally:
        db.close()


@app.command()
def serve(
    host: str = "0.0.0.0",
    port: int = 8000,
    reload: bool = False,
) -> None:
    """Start the Helix API + web console."""
    import uvicorn

    uvicorn.run("helix.api.main:app", host=host, port=port, reload=reload)


@app.command()
def health() -> None:
    s = get_settings()
    console.print({
        "env": s.helix_env,
        "llm_provider": s.llm_provider,
        "model": s.llm_model,
        "database": s.database_url.split("@")[-1] if "@" in s.database_url else s.database_url,
        "base_url": s.helix_base_url,
    })


if __name__ == "__main__":
    app()
