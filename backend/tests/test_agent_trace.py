from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient

from backend.app.agent.api import create_agent_router
from backend.app.agent.bootstrap import AgentApiContext
from backend.app.agent.context import DecisionLedger, FactIndex, MainModelContext, WorkingSet
from backend.app.agent.graph import (
    AnalysisExecutionRequest,
    AgentDecision,
    GraphState,
    MainModelOutput,
)
from backend.app.agent.model import VllmGraphModel
from backend.app.agent.model import ModelBoundaryError
from backend.app.agent.product_store import InMemoryAgentProductStore
from backend.app.agent.queue import AgentTurnWorkItem, InMemoryAgentTurnQueue
from backend.app.agent.runtime import AgentRuntime
from backend.app.agent.schemas import AgentThreadRecord
from backend.app.agent.trace import TraceRecorder
from backend.app.agent.param_resolver import ContrastSpec, DEGParams


COOKIE = "omicsprism_session"


class _Graph:
    def __init__(self) -> None:
        self.states: dict[str, GraphState] = {}

    def update_state(self, config: dict, values: dict) -> None:
        thread_id = config["configurable"]["thread_id"]
        self.states[thread_id] = GraphState.model_validate(values)

    def get_state(self, config: dict) -> SimpleNamespace:
        thread_id = config["configurable"]["thread_id"]
        state = self.states[thread_id]
        return SimpleNamespace(
            values=state,
            next=("main",) if state.response_text is None else (),
            tasks=(),
        )

    def invoke(self, state: GraphState | None, config: dict) -> None:
        thread_id = config["configurable"]["thread_id"]
        current = GraphState.model_validate(state) if state is not None else self.states[thread_id]
        self.states[thread_id] = current.model_copy(update={"response_text": "trace complete"})


class _Jobs:
    def get_for_user(self, _job_id: str, _user_id: str):
        raise KeyError("not found")


def _session(request: Request, response: Response) -> str:
    user_id = request.cookies.get(COOKIE)
    if not user_id:
        user_id = "trace-user"
        response.set_cookie(COOKIE, user_id, httponly=True, samesite="lax")
    return user_id


def _context() -> tuple[AgentApiContext, InMemoryAgentProductStore, InMemoryAgentTurnQueue, list]:
    store = InMemoryAgentProductStore()
    queue = InMemoryAgentTurnQueue()
    events = []
    def sink(event) -> None:
        events.append(event)
        store.record_trace_event(event)
    recorder = TraceRecorder(sink)
    context = AgentApiContext(
        product_store=store,
        job_store=_Jobs(),
        graph=_Graph(),
        files=None,
        turn_queue=queue,
        trace_recorder=recorder,
    )
    return context, store, queue, events


def _model_context() -> MainModelContext:
    return MainModelContext(
        trace_id="trace-model",
        thread_id="thread-model",
        turn_id="turn-model",
        run_id="run-model",
        user_id="user-a",
        user_message="hello",
        fact_index=FactIndex(context_version="facts.v1:test"),
        decision_ledger=DecisionLedger(context_version="ledger.v1:test"),
        working_set=WorkingSet(context_version="working.v1:test"),
    )


def test_api_queue_runtime_share_one_trace_id() -> None:
    context, store, queue, events = _context()
    app = FastAPI()
    app.include_router(create_agent_router(context=context, session_dependency=_session))
    client = TestClient(app)

    thread = client.post("/api/agent/threads", json={"focus_job_ids": []})
    assert thread.status_code == 201
    thread_id = thread.json()["thread_id"]
    response = client.post(
        f"/api/agent/threads/{thread_id}/turns",
        headers={"Idempotency-Key": "trace-turn"},
        json={"message": "hello"},
    )
    assert response.status_code == 202
    trace_id = response.json()["turn"]["trace_id"]

    raw = queue.reserve()
    assert raw is not None
    item = AgentTurnWorkItem.model_validate_json(raw)
    assert item.trace_id == trace_id
    AgentRuntime(context, queue).run_once(raw)

    recorded = store.list_trace_events(trace_id=trace_id, user_id="trace-user")
    assert [event.event_type for event in recorded] == [
        "turn.queued",
        "turn.started",
        "turn.completed",
    ]
    assert {event.trace_id for event in recorded} == {trace_id}
    assert recorded[-1].outcome == "completed"


def test_duplicate_delivery_keeps_trace_and_does_not_rerun_completed_turn() -> None:
    context, store, queue, events = _context()
    now = datetime.now(timezone.utc)
    store.save_thread(AgentThreadRecord(
        thread_id="thread-1",
        user_id="user-a",
        title="trace",
        current_run_id="run-1",
        status="active",
        version=0,
        created_at=now,
        updated_at=now,
    ))
    from backend.app.agent.schemas import AgentTurnRecord

    turn = AgentTurnRecord(
        turn_id="turn-1",
        thread_id="thread-1",
        run_id="run-1",
        user_id="user-a",
        trace_id="trace-1",
        idempotency_key="once",
        request_hash="sha256:" + "1" * 64,
        status="queued",
        attempt=0,
        error_code=None,
        created_at=now,
        updated_at=now,
        started_at=None,
        completed_at=None,
    )
    store.create_turn(turn)
    item = AgentTurnWorkItem(
        turn_id=turn.turn_id,
        thread_id=turn.thread_id,
        trace_id=turn.trace_id,
        user_id=turn.user_id,
        state=GraphState(
            thread_id=turn.thread_id,
            user_id=turn.user_id,
            trace_id=turn.trace_id,
            turn_id=turn.turn_id,
            user_message="hello",
        ),
    )
    raw = item.model_dump_json()
    AgentRuntime(context, queue).run_once(raw)
    AgentRuntime(context, queue).run_once(raw)

    recorded = store.list_trace_events(trace_id="trace-1", user_id="user-a")
    assert [event.event_type for event in recorded] == ["turn.started", "turn.completed"]
    assert all(event.trace_id == "trace-1" for event in recorded)


def test_vllm_usage_is_recorded_without_raw_prompt() -> None:
    events = []

    def handle(_request: httpx.Request) -> httpx.Response:
        output = {"decision": {"action": "answer"}, "answer": "bounded"}
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps(output)}}],
            "usage": {
                "prompt_tokens": 31,
                "completion_tokens": 7,
                "total_tokens": 38,
                "prompt_tokens_details": {"cached_tokens": 4},
            },
        })

    context = _model_context()
    model = VllmGraphModel(
        base_url="http://model-host:8000",
        model="Qwen3",
        client=httpx.Client(transport=httpx.MockTransport(handle)),
        trace_recorder=TraceRecorder(events.append),
    )
    result = model(context)

    assert isinstance(result, MainModelOutput)
    assert model.last_usage.prompt_tokens == 31
    assert model.last_usage.completion_tokens == 7
    assert model.last_usage.total_tokens == 38
    assert model.last_usage.cached_tokens == 4
    event = events[0]
    assert event.event_type == "model.call"
    assert event.usage_status == "reported"
    assert event.total_tokens == 38
    serialized = json.dumps(event.model_dump(mode="json"), ensure_ascii=False)
    assert "hello" not in serialized


def test_vllm_missing_usage_is_unknown_and_null() -> None:
    events = []

    def handle(_request: httpx.Request) -> httpx.Response:
        output = {"decision": {"action": "answer"}, "answer": "bounded"}
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps(output)}}],
        })

    model = VllmGraphModel(
        base_url="http://model-host:8000",
        model="Qwen3",
        client=httpx.Client(transport=httpx.MockTransport(handle)),
        trace_recorder=TraceRecorder(events.append),
    )
    model(_model_context())

    usage = model.last_usage
    assert usage.status == "unknown"
    assert usage.prompt_tokens is None
    assert usage.completion_tokens is None
    assert usage.total_tokens is None
    assert events[0].usage_status == "unknown"
    assert events[0].total_tokens is None


def test_rejected_model_output_is_not_written_to_logs(caplog) -> None:
    secret_output = "model-output-must-not-be-logged"

    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps({
                "decision": {"action": "answer"},
                "answer": None,
                "untrusted_note": secret_output,
            })}}],
        })

    model = VllmGraphModel(
        base_url="http://model-host:8000",
        model="Qwen3",
        client=httpx.Client(transport=httpx.MockTransport(handle)),
    )
    with caplog.at_level(logging.WARNING, logger="omicsprism.platform.agent_model"):
        try:
            model(_model_context())
        except ModelBoundaryError:
            pass
        else:  # pragma: no cover - assertion guard
            raise AssertionError("invalid model output must be rejected")

    assert secret_output not in caplog.text


def test_trace_query_is_owner_bound_and_events_have_no_tool_arguments() -> None:
    store = InMemoryAgentProductStore()
    now = datetime.now(timezone.utc)
    for user_id, thread_id in (("user-a", "thread-a"), ("user-b", "thread-b")):
        store.save_thread(AgentThreadRecord(
            thread_id=thread_id,
            user_id=user_id,
            title="trace",
            current_run_id=f"run-{user_id}",
            status="active",
            version=0,
            created_at=now,
            updated_at=now,
        ))
    recorder = TraceRecorder(store.record_trace_event)
    context = GraphState(
        thread_id="thread-a",
        user_id="user-a",
        trace_id="trace-shared",
        turn_id="turn-a",
        run_id="run-user-a",
        user_message="secret prompt",
    )
    recorder.tool_call(
        context=context,
        tool_name="describe_metadata",
        tool_schema_hash="sha256:" + "a" * 64,
        latency_ms=1.0,
        outcome="ok",
    )
    request = AnalysisExecutionRequest(
        user_id="user-a",
        thread_id="thread-a",
        trace_id="trace-shared",
        turn_id="turn-a",
        run_id="run-user-a",
        dataset_ids=["file-a"],
        resolved_params=DEGParams(contrast=ContrastSpec(
            compare_field="condition",
            tested_level="salt",
            reference_level="control",
        )),
        input_fingerprint="sha256:" + "b" * 64,
        idempotency_key="job-once",
    )
    recorder.job_submitted(request=request, job_id="job-a", latency_ms=2.0, outcome="submitted")
    recorder.turn_event(
        event_type="turn.queued",
        trace_id="trace-shared",
        thread_id="thread-b",
        turn_id="turn-b",
        run_id="run-user-b",
        user_id="user-b",
        outcome="queued",
    )

    own = store.list_trace_events(trace_id="trace-shared", user_id="user-a")
    assert [event.event_type for event in own] == ["tool.call", "job.submitted"]
    assert own[1].turn_id == "turn-a"
    assert own[1].run_id == "run-user-a"
    assert all("arguments" not in event.model_dump(mode="json") for event in own)
    assert store.list_trace_events(trace_id="trace-shared", user_id="user-b")[0].user_id == "user-b"
