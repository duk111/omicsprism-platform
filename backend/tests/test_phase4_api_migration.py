from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256

from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient

from backend.app.agent.api import create_agent_router
from backend.app.agent.bootstrap import AgentApiContext
from backend.app.agent.graph import (
    AgentDecision,
    AnalysisExecutionRequest,
    DatasetLoadRequest,
    JobRef,
    MainModelOutput,
    build_agent_graph,
)
from backend.app.agent.param_resolver import AnalysisProposal, ScopeSpec
from backend.app.agent.product_store import InMemoryAgentProductStore
from backend.app.agent.schemas import AgentInputBundleRecord, AgentInputFileRecord
from backend.app.agent.validation import DatasetRef


COOKIE = "omicsprism_session"
COUNTS = b"gene,s1,s2,s3,s4,s5,s6\ng1,10,12,30,32,20,22\n"
METADATA = (b"sample_id,condition\n"
            b"s1,control\ns2,control\ns3,salt\ns4,salt\ns5,drought\ns6,drought\n")


class _Jobs:
    def get_for_user(self, job_id: str, user_id: str):
        return object()


class _Model:
    def __init__(self, proposal: AnalysisProposal) -> None:
        self.proposal = proposal

    def __call__(self, _context):
        decision = AgentDecision(
            action="run_analysis", analysis_type="DEG", proposal=self.proposal)
        return MainModelOutput(decision=decision)


class _Submitter:
    def __init__(self) -> None:
        self.requests: list[AnalysisExecutionRequest] = []

    def __call__(self, request: AnalysisExecutionRequest) -> JobRef:
        self.requests.append(request)
        return JobRef(job_id="job-1", owner_id=request.user_id)


def _session(request: Request, response: Response) -> str:
    user_id = request.cookies.get(COOKIE) or "user-a"
    response.set_cookie(COOKIE, user_id)
    return user_id


def _setup(proposal: AnalysisProposal):
    refs = [
        DatasetRef(
            dataset_id=f"file-{role}", owner_id="user-a", role=role,
            filename=f"{role}.csv", checksum="sha256:" + sha256(content).hexdigest(),
            content=content)
        for role, content in (("counts", COUNTS), ("metadata", METADATA))
    ]

    def load(request: DatasetLoadRequest) -> list[DatasetRef]:
        assert request.user_id == "user-a"
        return [item.model_copy(deep=True) for item in refs if item.dataset_id in request.dataset_ids]

    submitter = _Submitter()
    unavailable = lambda request: (_ for _ in ()).throw(LookupError(request.job_id))
    graph = build_agent_graph(_Model(proposal), load, submitter, unavailable, unavailable)
    context = AgentApiContext(
        product_store=InMemoryAgentProductStore(),
        job_store=_Jobs(), files=None, graph=graph, dataset_loader=load)
    app = FastAPI()
    app.include_router(create_agent_router(context=context, session_dependency=_session))
    client = TestClient(app)
    client.cookies.set(COOKIE, "user-a")
    thread = client.post(
        "/api/agent/threads", json={"focus_job_ids": ["job-existing"]}).json()
    now = datetime.now(timezone.utc)
    context.product_store.save_input_bundle_with_files(
        bundle=AgentInputBundleRecord(
            bundle_id="bundle-1", thread_id=thread["thread_id"], user_id="user-a",
            status="active", expires_at=now + timedelta(hours=1), created_at=now),
        files=[
            AgentInputFileRecord(
                file_id=ref.dataset_id, bundle_id="bundle-1", user_id="user-a",
                field=ref.role, filename=ref.filename, storage_key=ref.dataset_id,
                checksum=ref.checksum, content_type="text/csv",
                size_bytes=len(ref.content), created_at=now)
            for ref in refs
        ]
    )
    return client, context, thread, submitter


def _start(client: TestClient, thread: dict, key: str = "turn-1"):
    return client.post(
        f"/api/agent/threads/{thread['thread_id']}/turns",
        headers={"Idempotency-Key": key},
        json={"message": "Run DEG", "input_bundle_id": "bundle-1"})


def _resume_url(thread: dict, body: dict) -> str:
    return (f"/api/agent/threads/{thread['thread_id']}/turns/"
            f"{body['checkpoint_turn_id']}/resume")


def test_confirmation_resume_uses_header_and_persists_completed_turn() -> None:
    client, context, thread, submitter = _setup(AnalysisProposal(
        analysis_type="DEG", compare_field="condition",
        tested_level="salt", reference_level="control",
        scope=ScopeSpec(mode="all"),
    ))
    paused = _start(client, thread)
    assert paused.status_code == 202
    body = paused.json()
    assert body["turn"]["status"] == "running"
    assert body["interrupt"]["payload"]["kind"] == "confirmation"
    snapshot = context.graph.get_state(
        {"configurable": {"thread_id": thread["thread_id"]}})
    assert snapshot.values["recent_jobs"][0].job_id == "job-existing"
    resume_url = _resume_url(thread, body)
    request = {
        "kind": "confirmation", "interrupt_id": body["interrupt"]["interrupt_id"],
        "plan_id": body["interrupt"]["payload"]["plan_id"],
        "plan_version": body["interrupt"]["payload"]["plan_version"],
        "approve": True,
    }
    assert client.post(resume_url, json=request).status_code == 422
    assert client.post(resume_url, json={**request, "interrupt_id": "stale"},
                       headers={"Idempotency-Key": "job-key"}).status_code == 409
    stale_version = {**request, "plan_version": request["plan_version"] + 1}
    assert client.post(
        resume_url, json=stale_version, headers={"Idempotency-Key": "job-key"}
    ).status_code == 409
    assert submitter.requests == []
    assert client.get(
        f"/api/agent/threads/{thread['thread_id']}/turns/{body['turn']['turn_id']}"
    ).json()["status"] == "running"

    completed = client.post(
        resume_url, json=request, headers={"Idempotency-Key": "job-key"})
    assert completed.status_code == 200
    assert completed.json()["turn"]["status"] == "completed"
    assert completed.json()["message"]["blocks"][0]["text"] == "Analysis job job-1 was submitted."
    assert [item.idempotency_key for item in submitter.requests] == ["job-key"]
    messages = context.product_store.list_messages(
        thread_id=thread["thread_id"], user_id="user-a")
    assert [item.role.value for item in messages] == ["user", "assistant"]
    replayed = _start(client, thread)
    assert replayed.status_code == 202
    assert replayed.json()["turn"]["status"] == "completed"
    assert client.post(resume_url, json=request,
                       headers={"Idempotency-Key": "job-key"}).status_code == 409
    assert len(submitter.requests) == 1


def test_clarification_resume_checks_ownership_and_preserves_missing_semantics() -> None:
    client, _context, thread, submitter = _setup(AnalysisProposal(
        analysis_type="DEG", compare_field="condition", scope=ScopeSpec(mode="all")
    ))
    body = _start(client, thread, "clarify-1").json()
    assert body["interrupt"]["payload"]["kind"] == "clarification"
    url = _resume_url(thread, body)
    request = {
        "kind": "clarification", "interrupt_id": body["interrupt"]["interrupt_id"],
        "answer": "compare salt and control",
    }
    client.cookies.clear()
    client.cookies.set(COOKIE, "user-b")
    assert client.post(url, json=request).status_code == 404
    client.cookies.clear()
    client.cookies.set(COOKIE, "user-a")
    resumed = client.post(url, json=request)
    assert resumed.status_code == 200
    assert resumed.json()["interrupt"]["payload"]["kind"] == "clarification"
    assert not submitter.requests


def test_openapi_exposes_typed_graph_resume_contract() -> None:
    client, _context, _thread, _submitter = _setup(AnalysisProposal(analysis_type="DEG", scope=ScopeSpec(mode="all")))
    schema = client.get("/openapi.json").json()
    path = "/api/agent/threads/{thread_id}/turns/{checkpoint_turn_id}/resume"
    request_schema = schema["paths"][path]["post"]["requestBody"]["content"][
        "application/json"]["schema"]
    assert request_schema["discriminator"]["propertyName"] == "kind"
    assert set(request_schema["discriminator"]["mapping"]) == {"clarification", "confirmation"}


def test_new_turn_releases_inline_turn_with_lost_checkpoint() -> None:
    proposal = AnalysisProposal(
        analysis_type="DEG", compare_field="condition",
        tested_level="salt", reference_level="control",
        scope=ScopeSpec(mode="all"),
    )
    client, context, thread, submitter = _setup(proposal)
    previous = _start(client, thread, "before-restart").json()["turn"]
    unavailable = lambda request: (_ for _ in ()).throw(LookupError(request.job_id))
    fresh_graph = build_agent_graph(
        _Model(proposal), context.dataset_loader, submitter, unavailable, unavailable)
    object.__setattr__(context, "graph", fresh_graph)

    restarted = _start(client, thread, "after-restart")

    assert restarted.status_code == 202
    assert restarted.json()["interrupt"]["payload"]["kind"] == "confirmation"
    failed = context.product_store.get_turn(turn_id=previous["turn_id"], user_id="user-a")
    assert failed.status.value == "failed"
    assert failed.error_code == "graph_checkpoint_unavailable"
