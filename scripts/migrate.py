from __future__ import annotations

import os
from pathlib import Path

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations"


def setup_agent_checkpointer(database_url: str) -> None:
    """Create LangGraph checkpoint tables with the migration administrator."""
    pool = ConnectionPool(
        database_url,
        min_size=1,
        max_size=1,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
        },
        open=False,
    )
    try:
        pool.open(wait=True)
        PostgresSaver(pool).setup()
    finally:
        pool.close()


def apply_migrations(database_url: str, app_password: str) -> list[str]:
    applied_now: list[str] = []
    setup_agent_checkpointer(database_url)
    with psycopg.connect(database_url) as conn:
        conn.execute(
            """
            create table if not exists schema_migrations (
                filename text primary key,
                applied_at timestamptz not null default now()
            )
            """
        )
        applied = {row[0] for row in conn.execute("select filename from schema_migrations")}
        for path in sorted(MIGRATIONS.glob("*.sql")):
            if path.name in applied:
                continue
            conn.execute("select set_config('omicsprism.app_db_password', %s, true)", (app_password,))
            conn.execute(path.read_text(encoding="utf-8"))
            conn.execute("insert into schema_migrations (filename) values (%s)", (path.name,))
            applied_now.append(path.name)
    return applied_now


if __name__ == "__main__":
    dsn = os.environ["OMICS_PRISM_MIGRATION_DATABASE_URL"]
    password = os.environ["OMICS_PRISM_APP_DB_PASSWORD"]
    for migration in apply_migrations(dsn, password):
        print(f"applied {migration}")
