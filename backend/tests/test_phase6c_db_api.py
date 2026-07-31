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
    reason="需要专用 PostgreSQL 测试库和 OMICS_PRISM_TEST_* 环境变量",
)
def test_postgres_agent_http_enqueue_is_atomic_idempotent_and_ownership_bound() -> None:
    import psycopg
    from fastapi import FastAPI, Request, Response
    from fastapi.testclient import TestClient

    from backend.app.agent.api import create_agent_router
    from backend.app.agent.approvals import PostgresApprovalGate
    from backend.app.agent.bootstrap import AgentApiContext
    from backend.app.agent.plans import PostgresPlanStore
    from backend.app.agent.product_store import PostgresAgentProductStore
    from backend.app.agent.store import PostgresStateStore
    from scripts.migrate import apply_migrations

    assert ADMIN_DSN and APP_DSN and APP_PASSWORD
    apply_migrations(ADMIN_DSN, APP_PASSWORD)
    suffix = str(uuid4())
    user_id = f"user-{suffix}"
    other_user_id = f"other-{suffix}"

    class _Jobs:
        def get_for_user(self, job_id: str, owner_id: str):
            raise LookupError(job_id)

    def session(request: Request, response: Response) -> str:
        return request.cookies.get("omicsprism_session") or "anonymous"

    context = AgentApiContext(
        product_store=PostgresAgentProductStore(APP_DSN),
        state_store=PostgresStateStore(APP_DSN),
        plan_store=PostgresPlanStore(APP_DSN),
        approval_gate=PostgresApprovalGate(APP_DSN),
        job_store=_Jobs(),
        files=None,
    )
    app = FastAPI()
    app.include_router(create_agent_router(context=context, session_dependency=session))
    client = TestClient(app)
    thread_id = None
    run_id = None
    turn_id = None
    try:
        created = client.post(
            "/api/agent/threads",
            cookies={"omicsprism_session": user_id},
            json={"focus_job_ids": []},
        )
        assert created.status_code == 201
        thread_id = created.json()["thread_id"]
        run_id = created.json()["current_run_id"]
        path = f"/api/agent/threads/{thread_id}/turns"
        first = client.post(
            path,
            cookies={"omicsprism_session": user_id},
            headers={"Idempotency-Key": f"key-{suffix}"},
            json={"message": "analyze"},
        )
        replay = client.post(
            path,
            cookies={"omicsprism_session": user_id},
            headers={"Idempotency-Key": f"key-{suffix}"},
            json={"message": "analyze"},
        )
        assert first.status_code == replay.status_code == 202
        assert first.json()["turn_id"] == replay.json()["turn_id"]
        turn_id = first.json()["turn_id"]
        assert client.get(
            f"/api/agent/threads/{thread_id}",
            cookies={"omicsprism_session": other_user_id},
        ).status_code == 404
        assert client.post(
            path,
            cookies={"omicsprism_session": user_id},
            headers={"Idempotency-Key": f"key-{suffix}"},
            json={"message": "changed"},
        ).status_code == 409
        assert client.post(
            path,
            cookies={"omicsprism_session": user_id},
            headers={"Idempotency-Key": f"active-{suffix}"},
            json={"message": "second active turn"},
        ).status_code == 409

        with psycopg.connect(APP_DSN) as conn:
            assert conn.execute(
                "select count(*) from agent_turns where thread_id = %s and user_id = %s",
                (thread_id, user_id),
            ).fetchone() == (1,)
            assert conn.execute(
                "select count(*) from agent_messages where thread_id = %s and user_id = %s",
                (thread_id, user_id),
            ).fetchone() == (1,)
            assert conn.execute(
                "select count(*) from jobs where owner_id = %s",
                (user_id,),
            ).fetchone() == (0,)
    finally:
        if thread_id and run_id:
            with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
                conn.execute("delete from agent_messages where thread_id = %s", (thread_id,))
                conn.execute("delete from agent_turns where thread_id = %s", (thread_id,))
                conn.execute("delete from agent_threads where thread_id = %s", (thread_id,))
                conn.execute("delete from agent_runs where run_id = %s and user_id = %s", (run_id, user_id))
