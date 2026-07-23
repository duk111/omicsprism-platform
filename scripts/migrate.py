from __future__ import annotations

import os
from pathlib import Path

import psycopg


ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations"


def apply_migrations(database_url: str, app_password: str) -> list[str]:
    applied_now: list[str] = []
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
