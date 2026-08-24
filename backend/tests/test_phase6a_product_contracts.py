from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from backend.app.agent.product_store import (
    AgentResourceNotFound,
    IdempotencyConflict,
    InMemoryAgentProductStore,
)
from backend.app.agent.schemas import (
    AgentInputBundleRecord,
    AgentMessageRecord,
    AgentThreadCreateRequest,
    AgentThreadRecord,
    AgentTurnCreateRequest,
    AgentTurnRecord,
)


def _thread() -> AgentThreadRecord:
    now = datetime.now(timezone.utc)
    return AgentThreadRecord(
        thread_id="thread-1",
        user_id="user-1",
        title="Salt stress analysis",
        current_run_id="run-1",
        status="active",
        version=0,
        created_at=now,
        updated_at=now,
    )


def _turn(*, request_hash: str = "sha256:request-a") -> AgentTurnRecord:
    now = datetime.now(timezone.utc)
    return AgentTurnRecord(
        turn_id="turn-1",
        thread_id="thread-1",
        run_id="run-1",
        user_id="user-1",
        idempotency_key="turn-key-1",
        request_hash=request_hash,
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


@pytest.mark.parametrize(("schema", "payload"), [
    (AgentThreadCreateRequest, {"focus_job_ids": []}),
    (AgentTurnCreateRequest, {"message": "analyze these files", "focus_job_ids": []}),
])
def test_agent_http_requests_reject_client_supplied_user_id(schema, payload) -> None:
    payload = {**payload, "user_id": "attacker"}

    with pytest.raises(ValidationError):
        schema.model_validate(payload)


def test_product_store_hides_cross_user_resources_as_not_found() -> None:
    store = InMemoryAgentProductStore()
    store.save_thread(_thread())
    store.append_message(AgentMessageRecord(
        message_id="message-1",
        thread_id="thread-1",
        run_id="run-1",
        user_id="user-1",
        role="user",
        blocks=[{"type": "text", "text": "analyze these files"}],
        created_at=datetime.now(timezone.utc),
    ))
    store.create_turn(_turn())
    store.save_input_bundle(AgentInputBundleRecord(
        bundle_id="bundle-1",
        thread_id="thread-1",
        user_id="user-1",
        status="active",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
        created_at=datetime.now(timezone.utc),
    ))

    with pytest.raises(AgentResourceNotFound):
        store.get_thread(thread_id="thread-1", user_id="user-2")
    with pytest.raises(AgentResourceNotFound):
        store.list_messages(thread_id="thread-1", user_id="user-2")
    with pytest.raises(AgentResourceNotFound):
        store.get_turn(turn_id="turn-1", user_id="user-2")
    with pytest.raises(AgentResourceNotFound):
        store.get_input_bundle(bundle_id="bundle-1", user_id="user-2")


def test_turn_idempotency_reuses_same_request_and_rejects_changed_request() -> None:
    store = InMemoryAgentProductStore()
    store.save_thread(_thread())

    first = store.create_turn(_turn())
    replay = store.create_turn(_turn())

    assert replay.turn_id == first.turn_id
    assert len(store.list_turns(thread_id="thread-1", user_id="user-1")) == 1

    with pytest.raises(IdempotencyConflict):
        store.create_turn(_turn(request_hash="sha256:request-b"))


def test_turn_idempotency_key_cannot_cross_thread_even_with_same_hash() -> None:
    store = InMemoryAgentProductStore()
    store.save_thread(_thread())
    store.create_turn(_turn())
    other_thread = _thread().model_copy(update={"thread_id": "thread-2", "current_run_id": "run-2"})
    store.save_thread(other_thread)

    with pytest.raises(IdempotencyConflict):
        store.create_turn(_turn().model_copy(update={"thread_id": "thread-2", "run_id": "run-2"}))
