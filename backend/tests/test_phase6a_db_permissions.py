from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
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
def test_phase6_runtime_role_ownership_idempotency_and_append_only_permissions() -> None:
    import psycopg
    from psycopg.errors import InsufficientPrivilege

    from backend.app.agent.audit import PostgresAgentEventStore
    from backend.app.agent.product_store import AgentResourceNotFound, IdempotencyConflict, PostgresAgentProductStore
    from backend.app.agent.schemas import (
        AgentEvent,
        AgentInputBundleRecord,
        AgentInputFileRecord,
        AgentMessageRecord,
        AgentThreadRecord,
        AgentTurnRecord,
    )
    from backend.app.agent.store import PostgresStateStore, StateNotFound
    from scripts.migrate import apply_migrations

    assert ADMIN_DSN and APP_DSN and APP_PASSWORD
    apply_migrations(ADMIN_DSN, APP_PASSWORD)
    suffix = str(uuid4())
    user_id = f"user-{suffix}"
    other_user_id = f"other-{suffix}"
    thread_id = f"thread-{suffix}"
    run_id = f"run-{suffix}"
    message_id = f"message-{suffix}"
    turn_id = f"turn-{suffix}"
    bundle_id = f"bundle-{suffix}"
    file_id = f"file-{suffix}"
    event_id = f"event-{suffix}"
    now = datetime.now(timezone.utc)

    try:
        with psycopg.connect(APP_DSN) as conn:
            conn.execute(
                """
                insert into agent_runs (
                    run_id, user_id, thread_id, active_profile, state, step_no,
                    focus, model_calls, tool_calls, status, version
                ) values (%s, %s, %s, 'analysis', 'COLLECT_INTENT', 0, %s, 0, 0, 'running', 0)
                """,
                (run_id, user_id, thread_id, psycopg.types.json.Jsonb({
                    "in_scope_job_ids": [], "resolved_entities": {}, "last_citation": None,
                })),
            )

        store = PostgresAgentProductStore(APP_DSN)
        store.save_thread(AgentThreadRecord(
            thread_id=thread_id,
            user_id=user_id,
            title="permission test",
            current_run_id=run_id,
            status="active",
            version=0,
            created_at=now,
            updated_at=now,
        ))
        store.append_message(AgentMessageRecord(
            message_id=message_id,
            thread_id=thread_id,
            run_id=run_id,
            user_id=user_id,
            role="user",
            blocks=[{"type": "text", "text": "hello"}],
            created_at=now,
        ))
        turn = AgentTurnRecord(
            turn_id=turn_id,
            thread_id=thread_id,
            run_id=run_id,
            user_id=user_id,
            idempotency_key=f"key-{suffix}",
            request_hash="sha256:a",
            status="queued",
            attempt=0,
            lease_owner=None,
            lease_expires_at=None,
            error_code=None,
            created_at=now,
            updated_at=now,
            started_at=None,
            completed_at=None,
        )
        assert store.create_turn(turn).turn_id == turn_id
        assert store.create_turn(turn).turn_id == turn_id
        with pytest.raises(IdempotencyConflict):
            store.create_turn(turn.model_copy(update={"request_hash": "sha256:b"}))
        with pytest.raises(AgentResourceNotFound):
            store.get_thread(thread_id=thread_id, user_id=other_user_id)
        with pytest.raises(AgentResourceNotFound):
            store.get_turn(turn_id=turn_id, user_id=other_user_id)

        store.save_input_bundle(AgentInputBundleRecord(
            bundle_id=bundle_id,
            thread_id=thread_id,
            user_id=user_id,
            status="active",
            expires_at=now + timedelta(hours=24),
            created_at=now,
        ))
        store.append_input_file(AgentInputFileRecord(
            file_id=file_id,
            bundle_id=bundle_id,
            user_id=user_id,
            field="counts",
            filename="counts.csv",
            storage_key=f"agent-inputs/{bundle_id}/counts.csv",
            checksum="sha256:fixture",
            content_type="text/csv",
            size_bytes=100,
            created_at=now,
        ))
        with pytest.raises(AgentResourceNotFound):
            store.get_input_bundle(bundle_id=bundle_id, user_id=other_user_id)

        state_store = PostgresStateStore(APP_DSN)
        assert state_store.get(run_id=run_id, user_id=user_id).run_id == run_id
        with pytest.raises(StateNotFound):
            state_store.get(run_id=run_id, user_id=other_user_id)

        event_store = PostgresAgentEventStore(APP_DSN)
        event_store.append(AgentEvent(
            event_id=event_id,
            run_id=run_id,
            user_id=user_id,
            step_no=0,
            event_type="phase6a.permission_test",
            payload={"status": "ok"},
        ))
        assert [event.event_id for event in event_store.list_for_run(run_id=run_id, user_id=user_id)] == [event_id]
        assert event_store.list_for_run(run_id=run_id, user_id=other_user_id) == []

        with psycopg.connect(APP_DSN) as conn:
            assert conn.execute("select count(*) from jobs where owner_id = %s", (user_id,)).fetchone() == (0,)

        with psycopg.connect(APP_DSN, autocommit=True) as conn:
            with pytest.raises(InsufficientPrivilege):
                conn.execute("update agent_messages set role = 'assistant' where message_id = %s", (message_id,))
        with psycopg.connect(APP_DSN, autocommit=True) as conn:
            with pytest.raises(InsufficientPrivilege):
                conn.execute("delete from agent_messages where message_id = %s", (message_id,))
        with psycopg.connect(APP_DSN, autocommit=True) as conn:
            with pytest.raises(InsufficientPrivilege):
                conn.execute("update agent_events set event_type = 'tampered' where event_id = %s", (event_id,))
        with psycopg.connect(APP_DSN, autocommit=True) as conn:
            with pytest.raises(InsufficientPrivilege):
                conn.execute("delete from agent_events where event_id = %s", (event_id,))
        with psycopg.connect(APP_DSN, autocommit=True) as conn:
            with pytest.raises(InsufficientPrivilege):
                conn.execute("delete from agent_turns where turn_id = %s and user_id = %s", (turn_id, user_id))
    finally:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
            for table, column, value in (
                ("agent_events", "event_id", event_id),
                ("agent_input_files", "file_id", file_id),
                ("agent_input_bundles", "bundle_id", bundle_id),
                ("agent_turns", "turn_id", turn_id),
                ("agent_messages", "message_id", message_id),
                ("agent_threads", "thread_id", thread_id),
            ):
                if value is not None:
                    conn.execute(f"delete from {table} where {column} = %s", (value,))
            conn.execute("delete from agent_runs where run_id = %s and user_id = %s", (run_id, user_id))
