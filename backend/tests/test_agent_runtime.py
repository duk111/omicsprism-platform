from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from psycopg import OperationalError

from backend.app.agent.bootstrap import AgentApiContext
from backend.app.agent.graph import (
    AgentDecision,
    DatasetProfileRef,
    GraphState,
    JobRef,
    MainModelOutput,
    build_agent_graph,
)
from backend.app.agent.dataset_profile import MatrixProfile
from backend.app.agent.product_store import InMemoryAgentProductStore
from backend.app.agent.job_events import (
    AgentJobCompletionEvent,
    AgentJobWaitRecord,
    AgentJobWaitStatus,
    completion_event_id,
    continuation_turn_id,
)
from backend.app.agent.queue import AgentTurnInput, AgentTurnWorkItem, InMemoryAgentTurnQueue
from backend.app.agent.runtime import AgentRuntime
from backend.app.agent.schemas import (
    AgentInputBundleRecord,
    AgentInputBundleStatus,
    AgentErrorBlock,
    AgentMessageRecord,
    AgentMessageRole,
    AgentTextBlock,
    AgentThreadRecord,
    AgentTurnRecord,
    AgentTurnStatus,
)
from backend.app.models import JobStatus
from langgraph.checkpoint.memory import InMemorySaver


class _Graph:
    def __init__(self, *, fail_once: bool = False) -> None:
        self.state: GraphState | None = None
        self.fail_once = fail_once

    def update_state(self, _config: dict, values: dict) -> None:
        self.state = GraphState.model_validate(values)

    def get_state(self, _config: dict) -> SimpleNamespace:
        return SimpleNamespace(
            values=self.state,
            next=("main",) if self.state and self.state.response_text is None else (),
            tasks=(),
        )

    def invoke(self, _input: object, _config: dict) -> None:
        if self.fail_once:
            self.fail_once = False
            raise KeyboardInterrupt()
        assert self.state is not None
        self.state = self.state.model_copy(update={"response_text": "runtime complete"})


class _GraphWithoutNextHint(_Graph):
    def get_state(self, _config: dict) -> SimpleNamespace:
        return SimpleNamespace(values=self.state, next=(), tasks=())


class _TransientGraph(_Graph):
    def __init__(self) -> None:
        super().__init__()
        self.fail_once = True

    def invoke(self, input_value: object, config: dict) -> None:
        if self.fail_once:
            self.fail_once = False
            raise OperationalError("connection reset")
        super().invoke(input_value, config)


class _BrokenGraph(_Graph):
    def update_state(self, _config: dict, values: dict) -> None:
        raise ValueError("invalid graph state")


def _context(graph: _Graph) -> tuple[AgentApiContext, InMemoryAgentTurnQueue, AgentTurnRecord]:
    now = datetime.now(timezone.utc)
    store = InMemoryAgentProductStore()
    store.save_thread(AgentThreadRecord(
        thread_id="thread-1",
        user_id="user-1",
        title="runtime",
        current_run_id="run-1",
        status="active",
        version=0,
        created_at=now,
        updated_at=now,
    ))
    turn = AgentTurnRecord(
        turn_id="turn-1",
        thread_id="thread-1",
        run_id="run-1",
        user_id="user-1",
        idempotency_key="turn-key",
        request_hash="sha256:request",
        status="queued",
        attempt=0,
        error_code=None,
        created_at=now,
        updated_at=now,
        started_at=None,
        completed_at=None,
    )
    store.create_turn(turn)
    queue = InMemoryAgentTurnQueue()
    context = AgentApiContext(
        product_store=store,
        job_store=object(),
        graph=graph,
        files=None,
        turn_queue=queue,
    )
    return context, queue, turn


def _state() -> GraphState:
    return GraphState(
        thread_id="thread-1",
        user_id="user-1",
        user_message="hello",
    )


def test_runtime_completes_queued_turn_and_persists_assistant_message() -> None:
    context, queue, turn = _context(_Graph())
    item = AgentTurnWorkItem(
        turn_id=turn.turn_id,
        thread_id=turn.thread_id,
        user_id=turn.user_id,
        state=_state(),
    )
    queue.enqueue(item)
    raw = queue.reserve()
    assert raw is not None

    AgentRuntime(context, queue).run_once(raw)

    completed = context.product_store.get_turn(turn_id=turn.turn_id, user_id=turn.user_id)
    assert completed.status.value == "completed"
    messages = context.product_store.list_messages(thread_id=turn.thread_id, user_id=turn.user_id)
    assert [block.text for block in messages[0].blocks] == ["runtime complete"]
    assert not queue.processing


def test_runtime_executes_job_continuation_and_closes_wait() -> None:
    context, queue, turn = _context(_Graph())
    now = datetime.now(timezone.utc)
    store = context.product_store
    running = store.claim_turn(turn_id=turn.turn_id, user_id=turn.user_id, now=now)
    store.finish_turn(
        turn_id=running.turn_id,
        user_id=running.user_id,
        status=AgentTurnStatus.COMPLETED,
        now=now,
    )
    store.create_job_wait(AgentJobWaitRecord(
        wait_id="wait-job-1",
        thread_id="thread-1",
        user_id="user-1",
        turn_id=turn.turn_id,
        run_id=turn.run_id,
        trace_id=turn.trace_id,
        job_id="job-1",
        created_at=now,
        updated_at=now,
    ))
    event = AgentJobCompletionEvent(
        event_id=completion_event_id("job-1", JobStatus.SUCCEEDED),
        job_id="job-1",
        thread_id="thread-1",
        user_id="user-1",
        turn_id=turn.turn_id,
        run_id=turn.run_id,
        trace_id=turn.trace_id,
        status=JobStatus.SUCCEEDED,
        occurred_at=now,
    )
    store.save_job_completion_event(event)
    continuation = store.prepare_job_continuation(event, now=now)
    assert continuation is not None
    context.graph.update_state({}, GraphState(
        thread_id="thread-1",
        user_id="user-1",
        trace_id="trace-1",
        turn_id=turn.turn_id,
        run_id=turn.run_id,
        user_message="Analysis job submitted.",
    ).model_dump(mode="json"))
    item = AgentTurnWorkItem(
        turn_id=continuation.turn_id,
        thread_id="thread-1",
        user_id="user-1",
        trace_id="trace-1",
        continuation=event,
    )

    AgentRuntime(context, queue).run_once(item.model_dump_json())

    completed = store.get_turn(
        turn_id=continuation_turn_id(event.event_id), user_id="user-1"
    )
    assert completed.status is AgentTurnStatus.COMPLETED
    wait = store.get_job_wait(job_id="job-1", user_id="user-1")
    assert wait.status is AgentJobWaitStatus.COMPLETED


def test_runtime_does_not_call_model_for_failed_job_continuation() -> None:
    context, queue, turn = _context(_Graph())
    now = datetime.now(timezone.utc)
    store = context.product_store
    running = store.claim_turn(turn_id=turn.turn_id, user_id=turn.user_id, now=now)
    store.finish_turn(
        turn_id=running.turn_id,
        user_id=running.user_id,
        status=AgentTurnStatus.COMPLETED,
        now=now,
    )
    store.create_job_wait(AgentJobWaitRecord(
        wait_id="wait-job-failed",
        thread_id="thread-1",
        user_id="user-1",
        turn_id=turn.turn_id,
        run_id=turn.run_id,
        trace_id=turn.trace_id,
        job_id="job-failed",
        created_at=now,
        updated_at=now,
    ))
    event = AgentJobCompletionEvent(
        event_id=completion_event_id("job-failed", JobStatus.FAILED),
        job_id="job-failed",
        thread_id="thread-1",
        user_id="user-1",
        turn_id=turn.turn_id,
        run_id=turn.run_id,
        trace_id=turn.trace_id,
        status=JobStatus.FAILED,
        error_code="analysis_failed",
        occurred_at=now,
    )
    store.save_job_completion_event(event)
    continuation = store.prepare_job_continuation(event, now=now)
    assert continuation is not None
    context.graph.update_state({}, GraphState(
        thread_id="thread-1", user_id="user-1", trace_id="trace-1",
        turn_id=turn.turn_id, run_id=turn.run_id, user_message="submitted",
    ).model_dump(mode="json"))
    AgentRuntime(context, queue).run_once(AgentTurnWorkItem(
        turn_id=continuation.turn_id,
        thread_id="thread-1",
        user_id="user-1",
        trace_id="trace-1",
        continuation=event,
    ).model_dump_json())
    messages = store.list_messages(thread_id="thread-1", user_id="user-1")
    assert "analysis_failed" in messages[-1].blocks[0].text


def test_runtime_explicitly_invokes_a_new_turn_after_a_completed_checkpoint() -> None:
    context, queue, turn = _context(_GraphWithoutNextHint())
    item = AgentTurnWorkItem(
        turn_id=turn.turn_id,
        thread_id=turn.thread_id,
        user_id=turn.user_id,
        state=_state(),
    )

    AgentRuntime(context, queue).run_once(item.model_dump_json())

    completed = context.product_store.get_turn(turn_id=turn.turn_id, user_id=turn.user_id)
    assert completed.status.value == "completed"
    assert context.product_store.list_messages(
        thread_id=turn.thread_id,
        user_id=turn.user_id,
    )


def test_runtime_runs_consecutive_turns_with_a_real_langgraph_checkpoint() -> None:
    class _AnswerModel:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, _context):
            self.calls += 1
            return MainModelOutput(
                decision=AgentDecision(action="answer"),
                answer=f"answer {self.calls}",
            )

    model = _AnswerModel()
    graph = build_agent_graph(
        model,
        lambda _request: [],
        lambda request: JobRef(job_id="job-1", owner_id=request.user_id),
        lambda _request: (_ for _ in ()).throw(LookupError()),
        lambda _request: (_ for _ in ()).throw(LookupError()),
        checkpointer=InMemorySaver(),
    )
    context, queue, first_turn = _context(graph)
    runtime = AgentRuntime(context, queue)

    runtime.run_once(AgentTurnWorkItem(
        turn_id=first_turn.turn_id,
        thread_id=first_turn.thread_id,
        user_id=first_turn.user_id,
        state=_state(),
    ).model_dump_json())

    now = datetime.now(timezone.utc)
    second_turn = first_turn.model_copy(update={
        "turn_id": "turn-2",
        "idempotency_key": "turn-key-2",
        "request_hash": "sha256:request-2",
        "status": AgentTurnStatus.QUEUED,
        "attempt": 0,
        "created_at": now,
        "updated_at": now,
    })
    context.product_store.create_turn(second_turn)
    runtime.run_once(AgentTurnWorkItem(
        turn_id=second_turn.turn_id,
        thread_id=second_turn.thread_id,
        user_id=second_turn.user_id,
        state=_state().model_copy(update={"user_message": "second turn"}),
    ).model_dump_json())

    assert model.calls == 2
    assert context.product_store.get_turn(
        turn_id=second_turn.turn_id,
        user_id=second_turn.user_id,
    ).status.value == "completed"
    messages = context.product_store.list_messages(
        thread_id=first_turn.thread_id,
        user_id=first_turn.user_id,
    )
    assert [block.text for message in messages for block in message.blocks] == [
        "answer 1",
        "answer 2",
    ]


def test_runtime_merges_new_input_and_retains_only_durable_context() -> None:
    class _AnswerModel:
        def __init__(self) -> None:
            self.contexts = []

        def __call__(self, context):
            self.contexts.append(context)
            return MainModelOutput(
                decision=AgentDecision(action="answer"),
                answer=f"answer {len(self.contexts)}",
            )

    model = _AnswerModel()
    graph = build_agent_graph(
        model,
        lambda _request: [],
        lambda request: JobRef(job_id="job-1", owner_id=request.user_id),
        lambda _request: (_ for _ in ()).throw(LookupError()),
        lambda _request: (_ for _ in ()).throw(LookupError()),
        checkpointer=InMemorySaver(),
    )
    context, queue, first_turn = _context(graph)
    runtime = AgentRuntime(context, queue)
    context.product_store.append_message(AgentMessageRecord(
        message_id="user-turn-1-input",
        thread_id="thread-1",
        run_id="run-1",
        user_id="user-1",
        role=AgentMessageRole.USER,
        blocks=[AgentTextBlock(text="first question")],
        created_at=datetime.now(timezone.utc),
    ))

    runtime.run_once(AgentTurnWorkItem(
        turn_id=first_turn.turn_id,
        thread_id=first_turn.thread_id,
        user_id=first_turn.user_id,
        input=AgentTurnInput(message="first question"),
    ).model_dump_json())

    now = datetime.now(timezone.utc)
    second_turn = first_turn.model_copy(update={
        "turn_id": "turn-2-input",
        "idempotency_key": "turn-key-2-input",
        "request_hash": "sha256:request-2-input",
        "status": AgentTurnStatus.QUEUED,
        "attempt": 0,
        "created_at": now,
        "updated_at": now,
    })
    context.product_store.create_turn(second_turn)
    context.product_store.append_message(AgentMessageRecord(
        message_id="user-turn-2-input",
        thread_id="thread-1",
        run_id="run-1",
        user_id="user-1",
        role=AgentMessageRole.USER,
        blocks=[AgentTextBlock(text="second question")],
        created_at=now,
    ))
    # A new runtime instance proves this turn only depends on the persisted
    # thread/checkpoint boundaries, not the first consumer's local variables.
    AgentRuntime(context, queue).run_once(AgentTurnWorkItem(
        turn_id=second_turn.turn_id,
        thread_id=second_turn.thread_id,
        user_id=second_turn.user_id,
        input=AgentTurnInput(message="second question"),
    ).model_dump_json())

    state = GraphState.model_validate(
        graph.get_state({"configurable": {"thread_id": "thread-1"}}).values
    )
    assert state.user_message == "second question"
    assert state.turn_id == second_turn.turn_id
    assert state.step_budget.used_model_steps == 1
    assert state.decision.action == "answer"
    assert [item.text for item in model.contexts[1].recent_messages.messages] == [
        "first question", "answer 1", "second question",
    ]


def test_runtime_retains_explicit_user_corrections_in_structured_memory() -> None:
    context, queue, turn = _context(_Graph())
    runtime = AgentRuntime(context, queue)
    runtime.run_once(AgentTurnWorkItem(
        turn_id=turn.turn_id,
        thread_id=turn.thread_id,
        user_id=turn.user_id,
        input=AgentTurnInput(message="first question"),
    ).model_dump_json())
    now = datetime.now(timezone.utc)
    follow_up = turn.model_copy(update={
        "turn_id": "turn-correction",
        "idempotency_key": "turn-correction-key",
        "request_hash": "sha256:turn-correction",
        "status": AgentTurnStatus.QUEUED,
        "attempt": 0,
        "created_at": now,
        "updated_at": now,
    })
    context.product_store.create_turn(follow_up)
    runtime.run_once(AgentTurnWorkItem(
        turn_id=follow_up.turn_id,
        thread_id=follow_up.thread_id,
        user_id=follow_up.user_id,
        input=AgentTurnInput(message="Please change reference to control instead."),
    ).model_dump_json())

    assert context.graph.state is not None
    assert context.graph.state.conversation_memory.user_corrections == [
        "Please change reference to control instead.",
    ]


def test_runtime_does_not_inherit_an_expired_input_bundle() -> None:
    context, queue, turn = _context(_Graph())
    now = datetime.now(timezone.utc)
    context.product_store.save_input_bundle(AgentInputBundleRecord(
        bundle_id="bundle-expired",
        thread_id=turn.thread_id,
        user_id=turn.user_id,
        status=AgentInputBundleStatus.EXPIRED,
        expires_at=now - timedelta(seconds=1),
        created_at=now - timedelta(hours=1),
    ))
    context.graph.state = _state().model_copy(update={
        "active_input_bundle_id": "bundle-expired",
    })

    AgentRuntime(context, queue).run_once(AgentTurnWorkItem(
        turn_id=turn.turn_id,
        thread_id=turn.thread_id,
        user_id=turn.user_id,
        input=AgentTurnInput(message="follow up without new files"),
    ).model_dump_json())

    assert context.graph.state is not None
    assert context.graph.state.active_input_bundle_id is None
    assert context.graph.state.dataset_profiles == []


def test_turn_input_rejects_profiles_without_their_bundle_reference() -> None:
    profile = DatasetProfileRef(
        dataset_id="counts-1",
        owner_id="user-1",
        filename="counts.csv",
        checksum="sha256:" + "a" * 64,
        profile=MatrixProfile(
            role="counts",
            shape=(1, 2),
            sample_ids=["s1", "s2"],
            feature_type="gene",
            feature_id_examples=["g1"],
            numeric_type="integer_counts",
            has_negative=False,
            missing_rate=0,
        ),
    )
    with pytest.raises(ValueError, match="bundle reference"):
        AgentTurnInput.model_validate({
            "message": "analyze",
            "dataset_profiles": [profile],
        })


def test_runtime_persists_model_boundary_fallback_as_assistant_message() -> None:
    class _RejectedModel:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, _context):
            self.calls += 1
            raise ValueError("invalid structured output")

    model = _RejectedModel()
    graph = build_agent_graph(
        model,
        lambda _request: [],
        lambda request: JobRef(job_id="job-1", owner_id=request.user_id),
        lambda _request: (_ for _ in ()).throw(LookupError()),
        lambda _request: (_ for _ in ()).throw(LookupError()),
        checkpointer=InMemorySaver(),
    )
    context, queue, turn = _context(graph)
    AgentRuntime(context, queue).run_once(AgentTurnWorkItem(
        turn_id=turn.turn_id,
        thread_id=turn.thread_id,
        user_id=turn.user_id,
        state=_state(),
    ).model_dump_json())

    completed = context.product_store.get_turn(
        turn_id=turn.turn_id,
        user_id=turn.user_id,
    )
    messages = context.product_store.list_messages(
        thread_id=turn.thread_id,
        user_id=turn.user_id,
    )
    assert completed.status.value == "completed"
    assert model.calls == 2
    assert len(messages) == 1
    assert messages[0].blocks[0].text


def test_runtime_retries_after_process_crash_from_same_checkpoint() -> None:
    context, queue, turn = _context(_Graph(fail_once=True))
    item = AgentTurnWorkItem(
        turn_id=turn.turn_id,
        thread_id=turn.thread_id,
        user_id=turn.user_id,
        state=_state(),
    )
    queue.enqueue(item)
    raw = queue.reserve()
    assert raw is not None
    with pytest.raises(KeyboardInterrupt):
        AgentRuntime(context, queue).run_once(raw)

    queue.recover_pending()
    retry_raw = queue.reserve()
    assert retry_raw == raw
    AgentRuntime(context, queue).run_once(retry_raw)

    completed = context.product_store.get_turn(turn_id=turn.turn_id, user_id=turn.user_id)
    assert completed.status.value == "completed"
    assert completed.attempt == 2
    assert len(context.product_store.list_messages(thread_id=turn.thread_id, user_id=turn.user_id)) == 1


def test_runtime_requeues_once_after_transient_database_failure() -> None:
    context, queue, turn = _context(_TransientGraph())
    item = AgentTurnWorkItem(
        turn_id=turn.turn_id,
        thread_id=turn.thread_id,
        user_id=turn.user_id,
        state=_state(),
    )
    queue.enqueue(item)
    raw = queue.reserve()
    assert raw is not None

    AgentRuntime(context, queue).run_once(raw)

    queued = context.product_store.get_turn(turn_id=turn.turn_id, user_id=turn.user_id)
    assert queued.status is AgentTurnStatus.QUEUED
    assert queued.attempt == 1
    assert queue.processing == []
    retry_raw = queue.reserve()
    assert retry_raw == raw

    AgentRuntime(context, queue).run_once(retry_raw)

    completed = context.product_store.get_turn(turn_id=turn.turn_id, user_id=turn.user_id)
    assert completed.status is AgentTurnStatus.COMPLETED
    assert completed.attempt == 2
    assert len(context.product_store.list_messages(thread_id=turn.thread_id, user_id=turn.user_id)) == 1


def test_runtime_persists_visible_error_message_for_failed_turn() -> None:
    context, queue, turn = _context(_BrokenGraph())

    AgentRuntime(context, queue).run_once(AgentTurnWorkItem(
        turn_id=turn.turn_id,
        thread_id=turn.thread_id,
        user_id=turn.user_id,
        state=_state(),
    ).model_dump_json())

    failed = context.product_store.get_turn(turn_id=turn.turn_id, user_id=turn.user_id)
    messages = context.product_store.list_messages(
        thread_id=turn.thread_id,
        user_id=turn.user_id,
    )
    assert failed.status is AgentTurnStatus.FAILED
    assert len(messages) == 1
    block = messages[0].blocks[0]
    assert isinstance(block, AgentErrorBlock)
    assert block.code == "agent_runtime_failed"
    assert block.retryable is True


def test_work_item_requires_exactly_one_operation() -> None:
    with pytest.raises(ValueError):
        AgentTurnWorkItem(
            turn_id="turn-1",
            thread_id="thread-1",
            user_id="user-1",
        )


def test_duplicate_delivery_after_completion_does_not_rerun_graph() -> None:
    graph = _Graph()
    context, queue, turn = _context(graph)
    item = AgentTurnWorkItem(
        turn_id=turn.turn_id,
        thread_id=turn.thread_id,
        user_id=turn.user_id,
        state=_state(),
    )
    raw = item.model_dump_json()
    AgentRuntime(context, queue).run_once(raw)
    assert graph.state is not None and graph.state.response_text == "runtime complete"

    AgentRuntime(context, queue).run_once(raw)

    assert graph.state is not None and graph.state.response_text == "runtime complete"
    assert len(context.product_store.list_messages(thread_id=turn.thread_id, user_id=turn.user_id)) == 1
