from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from backend.app.agent.bootstrap import AgentApiContext
from backend.app.agent.graph import GraphState
from backend.app.agent.product_store import InMemoryAgentProductStore
from backend.app.agent.queue import AgentTurnWorkItem, InMemoryAgentTurnQueue
from backend.app.agent.runtime import AgentRuntime
from backend.app.agent.schemas import AgentThreadRecord, AgentTurnRecord


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
