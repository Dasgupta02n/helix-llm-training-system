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
                "last_login_at": "TIMESTAMP",
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

        # training_examples ownership
        te_cols = _column_names(engine, "training_examples")
        if te_cols and "owner_user_id" not in te_cols:
            conn.execute(
                text("ALTER TABLE training_examples ADD COLUMN owner_user_id VARCHAR(32)")
            )
