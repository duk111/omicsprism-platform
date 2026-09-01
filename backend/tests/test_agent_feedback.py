from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient

from backend.app.agent.api import create_agent_router
from backend.app.agent.bootstrap import AgentApiContext
from backend.app.agent.feedback import (
    export_eval_candidate,
    redact_candidate_text,
)
from backend.app.agent.graph import GraphState
from backend.app.agent.product_store import AgentResourceNotFound, InMemoryAgentProductStore
from backend.app.agent.queue import InMemoryAgentTurnQueue
from backend.app.agent.runtime import AgentRuntime
from backend.app.agent.schemas import (
    AgentFeedbackCreateRequest,
    AgentFeedbackRating,
    AgentMessageRecord,
    AgentThreadRecord,
)
from backend.app.agent.trace import AgentTraceEvent


COOKIE = "omicsprism_session"


class _Graph:
    def __init__(self) -> None:
        self.states: dict[str, GraphState] = {}

    def update_state(self, config: dict, values: dict) -> None:
        self.states[config["configurable"]["thread_id"]] = GraphState.model_validate(values)

    def get_state(self, config: dict):
        state = self.states[config["configurable"]["thread_id"]]
        return type("Snapshot", (), {"values": state, "next": ("main",) if state.response_text is None else (), "tasks": ()})()

    def invoke(self, state: GraphState | None, config: dict) -> None:
        thread_id = config["configurable"]["thread_id"]
        current = GraphState.model_validate(state) if state is not None else self.states[thread_id]
        self.states[thread_id] = current.model_copy(update={"response_text": "assistant result"})


class _Jobs:
    def get_for_user(self, _job_id: str, _user_id: str):
        raise KeyError(_job_id)


def _session(request: Request, response: Response) -> str:
    user_id = request.cookies.get(COOKIE)
    if not user_id:
        user_id = "user-a"
        response.set_cookie(COOKIE, user_id, httponly=True, samesite="lax")
    return user_id


def _context() -> AgentApiContext:
    return AgentApiContext(
        product_store=InMemoryAgentProductStore(),
        job_store=_Jobs(),
        graph=_Graph(),
        files=None,
        turn_queue=InMemoryAgentTurnQueue(),
    )


def _client(context: AgentApiContext) -> TestClient:
    app = FastAPI()
    app.include_router(create_agent_router(context=context, session_dependency=_session))
    return TestClient(app)


def _as_user(client: TestClient, user_id: str) -> None:
    client.cookies.set(COOKIE, user_id)


def _create_thread(client: TestClient, user_id: str = "user-a") -> str:
    _as_user(client, user_id)
    response = client.post("/api/agent/threads", json={"focus_job_ids": []})
    assert response.status_code == 201
    return response.json()["thread_id"]


def _complete_turn(context: AgentApiContext, client: TestClient, thread_id: str) -> tuple[str, str]:
    response = client.post(
        f"/api/agent/threads/{thread_id}/turns",
        headers={"Idempotency-Key": "feedback-turn"},
        json={"message": "compare samples"},
    )
    assert response.status_code == 202
    raw = context.turn_queue.reserve()
    assert raw is not None
    AgentRuntime(context, context.turn_queue).run_once(raw)
    turn = response.json()["turn"]
    return turn["turn_id"], turn["trace_id"]


def test_feedback_schema_requires_category_only_for_unhelpful() -> None:
    with pytest.raises(ValueError):
        AgentFeedbackCreateRequest(rating="unhelpful")
    with pytest.raises(ValueError):
        AgentFeedbackCreateRequest(rating="helpful", failure_category="other")
    assert AgentFeedbackCreateRequest(rating="helpful").rating is AgentFeedbackRating.HELPFUL


def test_feedback_is_owned_upserted_and_persisted_as_redacted_candidate() -> None:
    context = _context()
    client = _client(context)
    thread_id = _create_thread(client)
    turn_id, trace_id = _complete_turn(context, client, thread_id)
    context.product_store.record_trace_event(AgentTraceEvent(
        event_id="trace-feedback", trace_id=trace_id, thread_id=thread_id, turn_id=turn_id,
        run_id=context.product_store.get_turn(turn_id=turn_id, user_id="user-a").run_id,
        user_id="user-a", event_type="model.call", component="model", name="chat.completions",
        schema_version="test.v1", latency_ms=14, created_at=datetime.now(timezone.utc),
    ))
    message_id = f"assistant-{turn_id}"
    first = client.put(
        f"/api/agent/threads/{thread_id}/messages/{message_id}/feedback",
        json={"rating": "unhelpful", "failure_category": "bad_plan", "correction_text": "Use the paired design."},
    )
    assert first.status_code == 200
    assert set(first.json()) == {"feedback_id", "message_id", "rating", "failure_category", "correction_text", "created_at", "updated_at"}
    pending = context.product_store.list_eval_candidates_for_review()
    assert len(pending) == 1
    candidate = pending[0]
    assert candidate.trace_summary.model_calls == 1
    exported = export_eval_candidate(candidate).model_dump()
    for private_key in ("user_id", "thread_id", "turn_id", "message_id", "trace_id", "feedback_id"):
        assert private_key not in exported

    second = client.put(
        f"/api/agent/threads/{thread_id}/messages/{message_id}/feedback",
        json={"rating": "helpful"},
    )
    assert second.status_code == 200
    assert second.json()["feedback_id"] == first.json()["feedback_id"]
    assert len(context.product_store.list_feedback(thread_id=thread_id, user_id="user-a")) == 1
    assert context.product_store.list_eval_candidates_for_review() == []
    with pytest.raises(AgentResourceNotFound):
        context.product_store.get_eval_candidate(
            candidate_id=candidate.candidate_id, user_id="user-a",
        )


def test_feedback_rejects_user_messages_cross_user_access_and_wrong_thread() -> None:
    context = _context()
    client = _client(context)
    thread_id = _create_thread(client)
    turn_id, _ = _complete_turn(context, client, thread_id)
    message_id = f"assistant-{turn_id}"
    body = {"rating": "unhelpful", "failure_category": "other"}
    assert client.put(f"/api/agent/threads/{thread_id}/messages/user-{turn_id}/feedback", json=body).status_code == 404
    other_thread = _create_thread(client, "user-b")
    assert client.put(f"/api/agent/threads/{other_thread}/messages/{message_id}/feedback", json=body).status_code == 404
    assert client.put(f"/api/agent/threads/{thread_id}/messages/{message_id}/feedback", json={**body, "turn_id": "attacker"}).status_code == 422


def test_deleting_a_thread_removes_feedback_and_candidate() -> None:
    context = _context()
    client = _client(context)
    thread_id = _create_thread(client)
    turn_id, _ = _complete_turn(context, client, thread_id)
    message_id = f"assistant-{turn_id}"
    response = client.put(
        f"/api/agent/threads/{thread_id}/messages/{message_id}/feedback",
        json={"rating": "unhelpful", "failure_category": "other"},
    )
    candidate_id = context.product_store.list_eval_candidates_for_review()[0].candidate_id
    assert client.delete(f"/api/agent/threads/{thread_id}").status_code == 204
    with pytest.raises(AgentResourceNotFound):
        context.product_store.get_eval_candidate(candidate_id=candidate_id, user_id="user-a")


def test_candidate_redaction_removes_sensitive_references_and_csv_rows() -> None:
    raw = (
        "postgresql://app:secret@10.1.2.3/db api_key=hidden s3://bucket/key \
        email@example.com C:\\work\\counts.csv /srv/private/file.csv"
    )
    redacted = redact_candidate_text(raw)
    for forbidden in ("postgresql://", "10.1.2.3", "hidden", "s3://", "email@example.com", "C:\\work", "/srv/private"):
        assert forbidden not in redacted
    assert redact_candidate_text("gene,s1\nA,12\nB,20\nC,8") == "[tabular content omitted]"
