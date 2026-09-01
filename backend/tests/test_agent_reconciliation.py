from datetime import datetime, timezone

from backend.app.agent.job_events import (
    AgentJobCompletionEvent,
    AgentJobWaitRecord,
    AgentJobWaitStatus,
    completion_event_id,
    continuation_turn_id,
)
from backend.app.agent.product_store import AgentResourceNotFound, InMemoryAgentProductStore
from backend.app.agent.queue import AgentTurnQueue, AgentTurnWorkItem, InMemoryAgentTurnQueue
from backend.app.agent.reconciliation import AgentJobEventReconciler
from backend.app.agent.schemas import AgentThreadRecord, AgentTurnRecord
from backend.app.models import JobStatus


def _store() -> InMemoryAgentProductStore:
    now = datetime.now(timezone.utc)
    store = InMemoryAgentProductStore()
    store.save_thread(AgentThreadRecord(
        thread_id="thread-1",
        user_id="user-1",
        title="reconciliation",
        current_run_id="run-1",
        status="active",
        version=0,
        created_at=now,
        updated_at=now,
    ))
    store.create_turn(AgentTurnRecord(
        turn_id="turn-1",
        thread_id="thread-1",
        run_id="run-1",
        user_id="user-1",
        trace_id="trace-1",
        idempotency_key="turn-key-1",
        request_hash="sha256:request",
        status="completed",
        attempt=1,
        error_code=None,
        created_at=now,
        updated_at=now,
        started_at=now,
        completed_at=now,
    ))
    store.create_job_wait(AgentJobWaitRecord(
        wait_id="wait-1",
        thread_id="thread-1",
        user_id="user-1",
        turn_id="turn-1",
        run_id="run-1",
        trace_id="trace-1",
        job_id="job-1",
        created_at=now,
        updated_at=now,
    ))
    return store


def _event(status: JobStatus = JobStatus.SUCCEEDED) -> AgentJobCompletionEvent:
    return AgentJobCompletionEvent(
        event_id=completion_event_id("job-1", status),
        job_id="job-1",
        thread_id="thread-1",
        user_id="user-1",
        turn_id="turn-1",
        run_id="run-1",
        trace_id="trace-1",
        status=status,
        occurred_at=datetime.now(timezone.utc),
    )


def test_reconciler_creates_one_continuation_and_marks_event_published() -> None:
    store = _store()
    event = _event()
    store.save_job_completion_event(event)
    queue = InMemoryAgentTurnQueue()
    reconciler = AgentJobEventReconciler(store, queue)

    assert reconciler.reconcile_once() == 1
    assert reconciler.reconcile_once() == 0
    assert len(queue.pending) == 1
    raw = queue.pending[0]
    item = AgentTurnWorkItem.model_validate_json(raw)
    assert item.turn_id == continuation_turn_id(event.event_id)
    assert item.continuation is not None
    wait = store.get_job_wait(job_id="job-1", user_id="user-1")
    assert wait.status is AgentJobWaitStatus.RESUME_QUEUED
    assert wait.continuation_turn_id == item.turn_id


def test_reconciler_duplicate_delivery_reuses_existing_turn() -> None:
    store = _store()
    event = _event()
    store.save_job_completion_event(event)
    first = store.prepare_job_continuation(event, now=datetime.now(timezone.utc))
    second = store.prepare_job_continuation(event, now=datetime.now(timezone.utc))
    assert first is not None
    assert second is not None
    assert second.turn_id == first.turn_id
    assert len(store.list_turns(thread_id="thread-1", user_id="user-1")) == 2


def test_cross_user_event_cannot_create_or_read_a_wait() -> None:
    store = _store()
    event = _event().model_copy(update={"user_id": "user-2"})
    try:
        store.save_job_completion_event(event)
        store.prepare_job_continuation(event, now=datetime.now(timezone.utc))
    except AgentResourceNotFound:
        pass
    else:
        raise AssertionError("cross-user event unexpectedly created a continuation")


class _FailingQueue(AgentTurnQueue):
    def enqueue(self, item: AgentTurnWorkItem) -> None:
        raise RuntimeError("redis unavailable")

    def recover_pending(self) -> None:
        pass

    def reserve(self, timeout_seconds: int = 5) -> str | None:
        return None

    def ack(self, raw_item: str) -> None:
        pass

    def retry(self, raw_item: str) -> None:
        pass


def test_reconciler_defers_when_agent_queue_is_unavailable() -> None:
    store = _store()
    event = _event()
    store.save_job_completion_event(event)
    reconciler = AgentJobEventReconciler(store, _FailingQueue())
    assert reconciler.reconcile_once() == 0
    pending = store.list_pending_job_events()
    assert len(pending) == 1
    assert pending[0].event_id == event.event_id
    assert pending[0].published_at is None
    assert pending[0].delivery_attempts == 1
    assert pending[0].last_error == "RuntimeError"
