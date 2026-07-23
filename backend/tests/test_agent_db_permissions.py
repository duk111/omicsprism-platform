from __future__ import annotations

import os
from uuid import uuid4

import pytest


ADMIN_DSN = os.getenv("OMICS_PRISM_TEST_DATABASE_URL")
APP_DSN = os.getenv("OMICS_PRISM_TEST_APP_DATABASE_URL")
APP_PASSWORD = os.getenv("OMICS_PRISM_APP_DB_PASSWORD")
HAS_TEST_DATABASE = bool(ADMIN_DSN and APP_DSN and APP_PASSWORD)


@pytest.mark.skipif(
    not HAS_TEST_DATABASE,
    reason=(
        "需要 OMICS_PRISM_TEST_DATABASE_URL、OMICS_PRISM_TEST_APP_DATABASE_URL "
        "和 OMICS_PRISM_APP_DB_PASSWORD 才能验证真实 PostgreSQL 权限"
    ),
)
def test_omics_app_can_append_but_cannot_mutate_agent_events() -> None:
    import psycopg
    from psycopg.errors import InsufficientPrivilege

    from scripts.migrate import apply_migrations

    assert ADMIN_DSN is not None
    assert APP_DSN is not None
    assert APP_PASSWORD is not None

    apply_migrations(ADMIN_DSN, APP_PASSWORD)
    run_id = f"run-{uuid4()}"
    user_id = f"user-{uuid4()}"
    event_id = f"event-{uuid4()}"

    try:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
            assert conn.execute(
                """
                select rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls
                from pg_roles where rolname = 'omics_app'
                """
            ).fetchone() == (False, False, False, False, False)

        with psycopg.connect(APP_DSN, autocommit=True) as conn:
            conn.execute(
                """
                insert into agent_runs (
                    run_id, user_id, thread_id, active_profile, state, step_no,
                    focus, model_calls, tool_calls, status, version
                ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    run_id,
                    user_id,
                    "thread-1",
                    "analysis",
                    "COLLECT_INTENT",
                    0,
                    psycopg.types.json.Jsonb({}),
                    0,
                    0,
                    "running",
                    0,
                ),
            )
            conn.execute(
                """
                insert into agent_events (event_id, run_id, user_id, step_no, event_type, payload)
                values (%s, %s, %s, %s, %s, %s)
                """,
                (event_id, run_id, user_id, 0, "run.created", psycopg.types.json.Jsonb({})),
            )
            assert conn.execute(
                "select event_id from agent_events where event_id = %s",
                (event_id,),
            ).fetchone() == (event_id,)
            conn.execute(
                "update agent_runs set version = 1 where run_id = %s and user_id = %s",
                (run_id, user_id),
            )

        with psycopg.connect(APP_DSN, autocommit=True) as conn:
            with pytest.raises(InsufficientPrivilege):
                conn.execute(
                    "update agent_events set event_type = 'tampered' where event_id = %s",
                    (event_id,),
                )

        with psycopg.connect(APP_DSN, autocommit=True) as conn:
            with pytest.raises(InsufficientPrivilege):
                conn.execute("delete from agent_events where event_id = %s", (event_id,))
    finally:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
            conn.execute("delete from agent_events where event_id = %s", (event_id,))
            conn.execute(
                "delete from agent_runs where run_id = %s and user_id = %s",
                (run_id, user_id),
            )
