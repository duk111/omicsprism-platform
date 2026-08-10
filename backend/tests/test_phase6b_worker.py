from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from backend.agent_worker import AgentWorker, _select_input_source
from backend.app.agent.model import ModelBoundaryError, ModelUnavailableError
from backend.app.agent.product_store import InMemoryAgentProductStore
from backend.app.agent.schemas import (
    ActiveProfile,
    AgentEvent,
    AgentInputSourceKind,
    AgentMessageRecord,
    AgentMessageRole,
    AgentState,
    AgentTextBlock,
    AgentThreadRecord,
    AgentTurnExecutionResult,
    AgentTurnRecord,
    AgentTurnStatus,
    RunFocus,
    RunState,
    RunStatus,
)
from backend.app.agent.validator import InvalidDecision


def _store(now: datetime) -> InMemoryAgentProductStore:
    store = InMemoryAgentProductStore()
    store.save_thread(AgentThreadRecord(
        thread_id="thread-1",
        user_id="user-1",
        title="worker test",
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
        idempotency_key="key-1",
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
    ))
    return store


class _Processor:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[str] = []

    def process(self, turn: AgentTurnRecord):
        self.calls.append(turn.turn_id)
        if self.error:
            raise self.error
        return [AgentTextBlock(text="done")]


def test_expired_lease_is_reclaimed_and_completed_without_duplicate_message() -> None:
    now = datetime.now(timezone.utc)
    store = _store(now)
    crashed = store.claim_next_turn(
        worker_id="crashed-worker",
        now=now,
        lease_seconds=10,
    )
    assert crashed is not None and crashed.attempt == 1

    processor = _Processor()
    worker = AgentWorker(
        store=store,
        processor=processor,
        worker_id="replacement-worker",
        lease_seconds=30,
        max_attempts=3,
        clock=lambda: now + timedelta(seconds=11),
    )

    assert worker.run_once()
    completed = store.get_turn(turn_id="turn-1", user_id="user-1")
    assert completed.status.value == "completed"
    assert completed.attempt == 2
    assert processor.calls == ["turn-1"]
    assert len(store.list_messages(thread_id="thread-1", user_id="user-1")) == 1


def test_model_failure_has_stable_error_and_no_processor_side_effect_retry() -> None:
    now = datetime.now(timezone.utc)
    store = _store(now)
    processor = _Processor(ModelUnavailableError("offline"))
    worker = AgentWorker(
        store=store,
        processor=processor,
        worker_id="worker-1",
        lease_seconds=30,
        max_attempts=3,
        clock=lambda: now,
    )

    assert worker.run_once()
    failed = store.get_turn(turn_id="turn-1", user_id="user-1")
    assert failed.status.value == "failed"
    assert failed.error_code == "model_unavailable"
    message = store.list_messages(thread_id="thread-1", user_id="user-1")[0]
    assert message.blocks[0].type == "error"
    assert message.blocks[0].retryable
    assert processor.calls == ["turn-1"]


def test_worker_slot_prevents_two_in_process_workers_from_claiming_concurrently() -> None:
    now = datetime.now(timezone.utc)
    store = _store(now)
    worker = AgentWorker(
        store=store,
        processor=_Processor(),
        worker_id="worker-2",
        clock=lambda: now,
    )

    with store.worker_slot() as acquired:
        assert acquired
        assert not worker.run_once()

    assert store.get_turn(turn_id="turn-1", user_id="user-1").status.value == "queued"


@pytest.mark.parametrize(
    ("error", "expected_code", "retryable"),
    [
        (httpx.ConnectError("connection refused"), "model_unavailable", True),
        (httpx.ReadTimeout("read timed out"), "model_timeout", True),
        (ModelBoundaryError("bad schema"), "invalid_model_response", True),
        (InvalidDecision("wrong action"), "model_decision_conflict", True),
        (
            httpx.HTTPStatusError(
                "service unavailable",
                request=httpx.Request("POST", "http://model/v1/chat/completions"),
                response=httpx.Response(503),
            ),
            "model_unavailable",
            True,
        ),
        (
            httpx.HTTPStatusError(
                "bad request",
                request=httpx.Request("POST", "http://model/v1/chat/completions"),
                response=httpx.Response(400),
            ),
            "model_request_rejected",
            False,
        ),
    ],
)
def test_model_transport_and_boundary_failures_have_stable_error_codes(
    error: Exception,
    expected_code: str,
    retryable: bool,
) -> None:
    now = datetime.now(timezone.utc)
    store = _store(now)
    worker = AgentWorker(
        store=store,
        processor=_Processor(error),
        worker_id="worker-errors",
        clock=lambda: now,
    )

    assert worker.run_once()

    turn = store.get_turn(turn_id="turn-1", user_id="user-1")
    block = store.list_messages(thread_id="thread-1", user_id="user-1")[0].blocks[0]
    assert turn.error_code == expected_code
    assert block.type == "error"
    assert block.code == expected_code
    assert block.retryable is retryable
    if isinstance(error, (ModelBoundaryError, InvalidDecision)):
        assert "未创建任务" in block.user_message


def test_execution_result_uses_atomic_checkpoint_commit() -> None:
    now = datetime.now(timezone.utc)

    class _AtomicStore(InMemoryAgentProductStore):
        def __init__(self, shared=None) -> None:
            super().__init__(shared)
            self.commits: list[dict[str, object]] = []

        def commit_turn_result(self, **kwargs):
            self.commits.append(kwargs)
            turn = kwargs["turn"]
            return self.finish_turn(
                turn_id=turn.turn_id,
                user_id=turn.user_id,
                worker_id=kwargs["worker_id"],
                status=AgentTurnStatus.COMPLETED,
                now=kwargs["now"],
            )

    class _ResultProcessor:
        def process(self, _turn: AgentTurnRecord) -> AgentTurnExecutionResult:
            return AgentTurnExecutionResult(
                state=RunState(
                    run_id="run-1",
                    user_id="user-1",
                    thread_id="thread-1",
                    active_profile=ActiveProfile.ANALYSIS,
                    state=AgentState.AWAIT_FOLLOWUP,
                    step_no=1,
                    plan_id=None,
                    plan_hash=None,
                    pending_approval_id=None,
                    focus=RunFocus(in_scope_job_ids=[], resolved_entities={}, last_citation=None),
                    model_calls=1,
                    tool_calls=1,
                    status=RunStatus.RUNNING,
                    version=1,
                ),
                blocks=[AgentTextBlock(text="done")],
                expected_version=0,
                events=[AgentEvent(
                    event_id="event-1",
                    run_id="run-1",
                    user_id="user-1",
                    step_no=1,
                    event_type="turn.completed",
                    payload={"turn_id": "turn-1"},
                )],
            )

    seeded = _store(now)
    store = _AtomicStore(seeded._shared)
    worker = AgentWorker(
        store=store,
        processor=_ResultProcessor(),
        worker_id="worker-atomic",
        clock=lambda: now,
    )

    assert worker.run_once()
    assert len(store.commits) == 1
    assert store.commits[0]["expected_version"] == 0
    assert store.get_turn(turn_id="turn-1", user_id="user-1").status.value == "completed"


def test_latest_bundle_replaces_rejected_plan_input_but_not_pending_plan_input() -> None:
    now = datetime.now(timezone.utc)
    state = RunState(
        run_id="run-1",
        user_id="user-1",
        thread_id="thread-1",
        active_profile=ActiveProfile.ANALYSIS,
        state=AgentState.NEED_USER_INPUT,
        plan_id="plan-old",
        plan_hash="sha256:old",
        pending_approval_id=None,
        focus=RunFocus(in_scope_job_ids=[], resolved_entities={}, last_citation=None),
        step_no=0,
        model_calls=0,
        tool_calls=0,
        status=RunStatus.RUNNING,
        version=1,
    )
    messages = [AgentMessageRecord(
        message_id="user-new-bundle",
        thread_id="thread-1",
        run_id="run-1",
        user_id="user-1",
        role=AgentMessageRole.USER,
        blocks=[{"type": "input_summary", "bundle_id": "bundle-new", "files": []}],
        created_at=now,
    )]

    class _Plans:
        def get(self, *, plan_id: str, user_id: str):
            assert (plan_id, user_id) == ("plan-old", "user-1")
            return type("StoredPlan", (), {
                "input_source": {"kind": "staged_bundle", "source_id": "bundle-old"},
            })()

    latest = _select_input_source(
        state=state,
        messages=messages,
        plan_store=_Plans(),
        user_id="user-1",
    )
    assert latest.kind is AgentInputSourceKind.STAGED_BUNDLE
    assert latest.source_id == "bundle-new"

    state.pending_approval_id = "approval-old"
    locked = _select_input_source(
        state=state,
        messages=messages,
        plan_store=_Plans(),
        user_id="user-1",
    )
    assert locked == {"kind": "staged_bundle", "source_id": "bundle-old"}
