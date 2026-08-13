"""Lightweight schema upgrades for existing SQLite/Postgres databases."""

from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


def _column_names(engine: Engine, table: str) -> set[str]:
    insp = inspect(engine)
    if table not in insp.get_table_names():
        return set()
    return {c["name"] for c in insp.get_columns(table)}


def apply_migrations(engine: Engine) -> None:
    """Add missing columns/tables that create_all won't alter on existing DBs."""
    dialect = engine.dialect.name
    bool_true = "BOOLEAN DEFAULT 1" if dialect == "sqlite" else "BOOLEAN DEFAULT TRUE"
    bool_false = "BOOLEAN DEFAULT 0" if dialect == "sqlite" else "BOOLEAN DEFAULT FALSE"

    with engine.begin() as conn:
        # topic_schemas enrichments
        cols = _column_names(engine, "topic_schemas")
        if cols:
            additions = {
                "display_name": "VARCHAR(200) DEFAULT ''",
                "description": "TEXT",
                "sample_row_json": "TEXT",
                "export_format": "VARCHAR(40) DEFAULT 'jsonl'",
                "is_active": bool_true,
                "updated_at": "TIMESTAMP",
            }
            for name, typ in additions.items():
                if name not in cols:
                    conn.execute(text(f"ALTER TABLE topic_schemas ADD COLUMN {name} {typ}"))

        # users enrichments for full auth
        user_cols = _column_names(engine, "users")
        if user_cols:
            user_additions = {
                "email_verified": bool_false,
                "password_set": bool_true,
                "admin_approved": bool_true,
                "last_login_at": "TIMESTAMP",
                "approved_at": "TIMESTAMP",
                "updated_at": "TIMESTAMP",
            }
            for name, typ in user_additions.items():
                if name not in user_cols:
                    conn.execute(text(f"ALTER TABLE users ADD COLUMN {name} {typ}"))
            # Existing users: treat as verified so current logins keep working
            if "email_verified" not in user_cols:
                if dialect == "sqlite":
                    conn.execute(text("UPDATE users SET email_verified = 1"))
                else:
                    conn.execute(text("UPDATE users SET email_verified = TRUE"))
            if "password_set" not in user_cols:
                if dialect == "sqlite":
                    conn.execute(text("UPDATE users SET password_set = 1"))
                else:
                    conn.execute(text("UPDATE users SET password_set = TRUE"))
            # Existing users: already active accounts stay approved
            if "admin_approved" not in user_cols:
                if dialect == "sqlite":
                    conn.execute(text("UPDATE users SET admin_approved = 1"))
                else:
                    conn.execute(text("UPDATE users SET admin_approved = TRUE"))

        # training_examples ownership
        te_cols = _column_names(engine, "training_examples")
        if te_cols and "owner_user_id" not in te_cols:
            conn.execute(
                text("ALTER TABLE training_examples ADD COLUMN owner_user_id VARCHAR(32)")
            )

        # corpus_documents: plan/project scoping (cross-plan contamination fix)
        corpus_cols = _column_names(engine, "corpus_documents")
        if corpus_cols and "project_id" not in corpus_cols:
            conn.execute(
                text("ALTER TABLE corpus_documents ADD COLUMN project_id VARCHAR(32)")
            )
            # Index for plan-scoped lookups (name may already exist on recreate)
            try:
                conn.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_corpus_documents_project_id "
                        "ON corpus_documents (project_id)"
                    )
                )
            except Exception:
                pass

        # Phase 0 cost tracking: split OpenRouter / Apify spend + job caps
        tenant_cols = _column_names(engine, "tenants")
        if tenant_cols:
            for name in ("openrouter_spent_usd", "apify_spent_usd"):
                if name not in tenant_cols:
                    conn.execute(
                        text(f"ALTER TABLE tenants ADD COLUMN {name} FLOAT DEFAULT 0")
                    )
            # Backfill: historical spent_usd was OpenRouter-only estimates
            if "openrouter_spent_usd" not in tenant_cols:
                conn.execute(
                    text(
                        "UPDATE tenants SET openrouter_spent_usd = COALESCE(spent_usd, 0) "
                        "WHERE COALESCE(openrouter_spent_usd, 0) = 0"
                    )
                )

        bj_cols = _column_names(engine, "batch_jobs")
        if bj_cols:
            bj_additions = {
                "openrouter_cost_usd": "FLOAT DEFAULT 0",
                "apify_cost_usd": "FLOAT DEFAULT 0",
                "cost_usd": "FLOAT DEFAULT 0",
                "target_gold": "INTEGER DEFAULT 0",
                "spend_cap_usd": "FLOAT DEFAULT 0",
                "spend_cap_override": bool_false,
            }
            for name, typ in bj_additions.items():
                if name not in bj_cols:
                    conn.execute(
                        text(f"ALTER TABLE batch_jobs ADD COLUMN {name} {typ}")
                    )

        gj_cols = _column_names(engine, "gather_jobs")
        if gj_cols and "cost_usd" not in gj_cols:
            conn.execute(
                text("ALTER TABLE gather_jobs ADD COLUMN cost_usd FLOAT DEFAULT 0")
            )

        ar_cols = _column_names(engine, "agent_runs")
        if ar_cols:
            ar_additions = {
                "cost_source": "VARCHAR(20)",
                "prompt_tokens": "INTEGER DEFAULT 0",
                "completion_tokens": "INTEGER DEFAULT 0",
            }
            for name, typ in ar_additions.items():
                if name not in ar_cols:
                    conn.execute(
                        text(f"ALTER TABLE agent_runs ADD COLUMN {name} {typ}")
                    )
