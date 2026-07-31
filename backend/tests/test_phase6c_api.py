from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient

from backend.app.agent.api import create_agent_router, project_stream_events
from backend.app.agent.approvals import InMemoryApprovalGate
from backend.app.agent.bootstrap import AgentApiContext
from backend.app.agent.plans import InMemoryPlanStore, compute_plan_hash
from backend.app.agent.product_store import InMemoryAgentProductStore
from backend.app.agent.schemas import (
    ActiveProfile,
    AgentInputBundleRecord,
    AgentMessageRecord,
    AgentState,
    AgentThreadRecord,
    AgentTurnRecord,
    PlanRecord,
    RunFocus,
    RunState,
    RunStatus,
)
from backend.app.agent.store import InMemoryStateStore
from backend.app.models import AnalysisType
from backend.app.settings import AppSettings
from backend.app.storage_service import FileStorageService


COOKIE = "omicsprism_session"


class _Jobs:
    def __init__(self) -> None:
        self.owners: dict[str, str] = {}
        self.created = 0

    def get_for_user(self, job_id: str, user_id: str):
        if self.owners.get(job_id) != user_id:
            raise KeyError(job_id)
        return object()


def _session(request: Request, response: Response) -> str:
    user_id = request.cookies.get(COOKIE)
    if not user_id:
        user_id = "anonymous-test-user"
        response.set_cookie(COOKIE, user_id, httponly=True, samesite="lax")
    return user_id


def _context() -> AgentApiContext:
    return AgentApiContext(
        product_store=InMemoryAgentProductStore(),
        state_store=InMemoryStateStore(),
        plan_store=InMemoryPlanStore(),
        approval_gate=InMemoryApprovalGate(),
        job_store=_Jobs(),
        files=None,
        stream_poll_seconds=0.01,
    )


def _client(context: AgentApiContext) -> TestClient:
    app = FastAPI()
    app.include_router(create_agent_router(context=context, session_dependency=_session))
    return TestClient(app)


def _as_user(client: TestClient, user_id: str) -> None:
    client.cookies.set(COOKIE, user_id)


def _create_thread(client: TestClient, user_id: str = "user-a") -> dict:
    _as_user(client, user_id)
    response = client.post(
        "/api/agent/threads",
        json={"focus_job_ids": []},
    )
    assert response.status_code == 201
    return response.json()


def test_request_body_rejects_user_id_and_cross_user_resources_are_404() -> None:
    context = _context()
    client = _client(context)
    _as_user(client, "user-a")
    assert client.post(
        "/api/agent/threads",
        json={"focus_job_ids": [], "user_id": "attacker"},
    ).status_code == 422

    thread = _create_thread(client)
    thread_id = thread["thread_id"]
    detail = client.get(f"/api/agent/threads/{thread_id}").json()
    assert detail["thread"]["thread_id"] == thread_id
    assert detail["run"]["run_id"] == thread["current_run_id"]
    assert "user_id" not in json_text(detail)
    turn = client.post(
        f"/api/agent/threads/{thread_id}/turns",
        headers={"Idempotency-Key": "turn-key-a"},
        json={"message": "analyze these files"},
    )
    assert turn.status_code == 202

    _as_user(client, "user-b")
    for path in (
        f"/api/agent/threads/{thread_id}",
        f"/api/agent/threads/{thread_id}/messages",
        f"/api/agent/threads/{thread_id}/turns/{turn.json()['turn_id']}",
    ):
        assert client.get(path).status_code == 404

    _as_user(client, "user-a")
    assert client.post(
        f"/api/agent/threads/{thread_id}/turns",
        headers={"Idempotency-Key": "body-user-id"},
        json={"message": "analyze", "user_id": "attacker"},
    ).status_code == 422


def test_turn_is_queued_atomically_and_idempotently_without_model_in_api() -> None:
    context = _context()
    assert "model" not in AgentApiContext.__dataclass_fields__
    client = _client(context)
    thread_id = _create_thread(client)["thread_id"]
    path = f"/api/agent/threads/{thread_id}/turns"
    headers = {"Idempotency-Key": "same-key"}
    payload = {"message": "run DEG"}
    _as_user(client, "user-a")

    first = client.post(path, headers=headers, json=payload)
    replay = client.post(path, headers=headers, json=payload)

    assert first.status_code == replay.status_code == 202
    assert first.json()["turn_id"] == replay.json()["turn_id"]
    messages = client.get(
        f"/api/agent/threads/{thread_id}/messages",
    ).json()["messages"]
    assert len(messages) == 1
    assert messages[0]["blocks"] == [{"type": "text", "text": "run DEG"}]

    conflict = client.post(
        path,
        headers=headers,
        json={"message": "run GMA"},
    )
    active = client.post(
        path,
        headers={"Idempotency-Key": "another-key"},
        json=payload,
    )
    assert conflict.status_code == 409
    assert active.status_code == 409


def test_bundle_is_ownership_bound_when_attached_to_turn() -> None:
    context = _context()
    client = _client(context)
    thread_id = _create_thread(client, "user-a")["thread_id"]
    context.product_store.save_input_bundle(AgentInputBundleRecord(
        bundle_id="bundle-a",
        thread_id=thread_id,
        user_id="user-a",
        status="active",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        created_at=datetime.now(timezone.utc),
    ))
    other_thread_id = _create_thread(client, "user-b")["thread_id"]
    _as_user(client, "user-b")

    response = client.post(
        f"/api/agent/threads/{other_thread_id}/turns",
        headers={"Idempotency-Key": "bundle-cross-user"},
        json={"message": "analyze", "input_bundle_id": "bundle-a"},
    )
    assert response.status_code == 404


def test_turn_focus_jobs_are_owned_and_persisted_before_worker_claim() -> None:
    context = _context()
    context.job_store.owners["job-a"] = "user-a"
    client = _client(context)
    thread = _create_thread(client, "user-a")
    accepted = client.post(
        f"/api/agent/threads/{thread['thread_id']}/turns",
        headers={"Idempotency-Key": "focus-owned"},
        json={"message": "interpret this result", "focus_job_ids": ["job-a"]},
    )
    assert accepted.status_code == 202
    state = context.state_store.get(run_id=thread["current_run_id"], user_id="user-a")
    assert state.focus.in_scope_job_ids == ["job-a"]

    other = _context()
    other.job_store.owners["job-a"] = "user-a"
    other_client = _client(other)
    other_thread = _create_thread(other_client, "user-b")
    denied = other_client.post(
        f"/api/agent/threads/{other_thread['thread_id']}/turns",
        headers={"Idempotency-Key": "focus-cross-user"},
        json={"message": "interpret", "focus_job_ids": ["job-a"]},
    )
    assert denied.status_code == 404
    assert other.product_store.list_turns(
        thread_id=other_thread["thread_id"], user_id="user-b",
    ) == []


def test_staged_csv_upload_returns_public_metadata_and_creates_no_job() -> None:
    with TemporaryDirectory(dir=Path.cwd()) as temp_dir:
        root = Path(temp_dir)
        context = _context()
        files = FileStorageService(AppSettings(
            base_dir=root,
            runs_dir=root / "runs",
            file_storage_root=root / "storage",
        ))
        context = replace(context, files=files)
        client = _client(context)
        thread_id = _create_thread(client, "user-a")["thread_id"]

        response = client.post(
            f"/api/agent/threads/{thread_id}/input-bundles",
            data={"fields": ["counts", "metadata"]},
            files=[
                ("files", ("counts.csv", b"gene,s1\nA,1\n", "text/csv")),
                ("files", ("metadata.csv", b"sample,group\ns1,salt\n", "text/csv")),
            ],
        )

        assert response.status_code == 201
        body = response.json()
        serialized = response.text
        assert [item["field"] for item in body["files"]] == ["counts", "metadata"]
        assert context.job_store.created == 0
        for secret_field in ("user_id", "storage_key", "path"):
            assert secret_field not in serialized
        stored = context.product_store.list_input_files(bundle_id=body["bundle_id"], user_id="user-a")
        assert all(item.storage_key.startswith("agent-inputs/") for item in stored)

        rejected = client.post(
            f"/api/agent/threads/{thread_id}/input-bundles",
            data={"fields": "counts", "user_id": "attacker"},
            files={"files": ("counts.csv", b"gene,s1\nA,1\n", "text/csv")},
        )
        assert rejected.status_code == 422


def test_stream_projection_contains_only_public_turn_and_message_dtos() -> None:
    now = datetime.now(timezone.utc)
    message = AgentMessageRecord(
        message_id="message-1",
        thread_id="thread-1",
        run_id="run-1",
        user_id="secret-user",
        role="assistant",
        blocks=[{"type": "text", "text": "done"}],
        created_at=now,
    )
    turn = AgentTurnRecord(
        turn_id="turn-1",
        thread_id="thread-1",
        run_id="run-1",
        user_id="secret-user",
        idempotency_key="secret-key",
        request_hash="sha256:secret",
        status="completed",
        attempt=1,
        lease_owner="secret-worker",
        lease_expires_at=None,
        error_code=None,
        created_at=now,
        updated_at=now,
        started_at=now,
        completed_at=now,
    )

    payload = "\n".join(event.model_dump_json() for event in project_stream_events([turn], [message]))
    assert '"event_type":"turn.updated"' in payload
    assert '"event_type":"message.created"' in payload
    for secret in ("secret-user", "secret-key", "secret-worker", "request_hash", "storage_key"):
        assert secret not in payload


def test_message_cursor_and_sse_snapshot_support_disconnect_recovery() -> None:
    context = _context()
    client = _client(context)
    thread = _create_thread(client, "user-a")
    turn = client.post(
        f"/api/agent/threads/{thread['thread_id']}/turns",
        headers={"Idempotency-Key": "recover-key"},
        json={"message": "analyze"},
    ).json()
    user_message_id = f"user-{turn['turn_id']}"
    context.product_store.append_message(AgentMessageRecord(
        message_id="assistant-recovery",
        thread_id=thread["thread_id"],
        run_id=thread["current_run_id"],
        user_id="user-a",
        role="assistant",
        blocks=[{"type": "text", "text": "queued"}],
        created_at=datetime.now(timezone.utc) + timedelta(milliseconds=1),
    ))

    recovered = client.get(
        f"/api/agent/threads/{thread['thread_id']}/messages",
        params={"after": user_message_id},
    )
    stream = client.get(
        f"/api/agent/threads/{thread['thread_id']}/stream",
        params={"once": "true"},
        headers={"Last-Event-ID": "event-outside-current-window"},
    )
    assert [item["message_id"] for item in recovered.json()["messages"]] == ["assistant-recovery"]
    assert stream.status_code == 200
    assert "event: turn.updated" in stream.text
    assert "event: message.created" in stream.text
    assert "recover-key" not in stream.text


def test_structured_approval_validates_owner_hash_and_queues_resume_turn() -> None:
    context = _context()
    client = _client(context)
    thread = _create_thread(client, "user-a")
    thread_id = thread["thread_id"]
    state = context.state_store.get(run_id=thread["current_run_id"], user_id="user-a")
    plan = PlanRecord(
        plan_id="plan-1",
        run_id=state.run_id,
        thread_id=thread_id,
        user_id="user-a",
        analysis_type=AnalysisType.DIFFERENTIAL,
        input_source={"kind": "existing_job", "source_id": "job-a"},
        requested_params={},
        effective_params={},
        contrasts=[{"compare_field": "group", "tested_level": "salt", "reference_level": "control"}],
        plan_hash="pending",
        approval_id=None,
    )
    plan.plan_hash = compute_plan_hash(plan)
    context.plan_store.save(plan)
    approval_id = context.approval_gate.suspend(
        run_id=state.run_id,
        user_id="user-a",
        plan_hash=plan.plan_hash,
        plan_id=plan.plan_id,
        thread_id=thread_id,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    plan.approval_id = approval_id
    context.plan_store.save(plan)
    state.plan_id = plan.plan_id
    state.plan_hash = plan.plan_hash
    state.pending_approval_id = approval_id
    state.state = AgentState.WAIT_EXECUTION_CONFIRMATION
    state.status = RunStatus.SUSPENDED
    context.state_store.save(state, expected_version=state.version)

    path = f"/api/agent/threads/{thread_id}/approvals/{approval_id}"
    _as_user(client, "user-b")
    assert client.post(
        path,
        json={"decision": "approve", "plan_hash": plan.plan_hash},
    ).status_code == 404
    _as_user(client, "user-a")
    assert client.post(
        path,
        json={"decision": "approve", "plan_hash": "sha256:wrong"},
    ).status_code == 409
    assert client.post(
        path,
        json={"decision": "approve", "plan_hash": plan.plan_hash, "user_id": "attacker"},
    ).status_code == 422
    assert context.job_store.created == 0

    approved = client.post(
        path,
        json={"decision": "approve", "plan_hash": plan.plan_hash},
    )
    assert approved.status_code == 202
    assert approved.json()["status"] == "queued"
    assert context.job_store.created == 0


def test_openapi_exposes_agent_contract_without_api_model_dependency() -> None:
    from backend.app import main

    schema = main.app.openapi()
    expected_paths = {
        "/api/agent/threads",
        "/api/agent/threads/{thread_id}",
        "/api/agent/threads/{thread_id}/messages",
        "/api/agent/threads/{thread_id}/turns",
        "/api/agent/threads/{thread_id}/turns/{turn_id}",
        "/api/agent/threads/{thread_id}/input-bundles",
        "/api/agent/threads/{thread_id}/approvals/{approval_id}",
        "/api/agent/threads/{thread_id}/stream",
    }
    assert expected_paths <= set(schema["paths"])
    request_properties = schema["components"]["schemas"]["AgentTurnCreateRequest"]["properties"]
    assert "user_id" not in request_properties
    generated = Path("frontend/src/api-types.ts").read_text(encoding="utf-8")
    assert "export interface AgentStreamEvent" in generated
    assert "blocks: (AgentTextBlock | AgentInputSummaryBlock" in generated
    for relative in ("backend/app/main.py", "backend/app/agent/api.py", "backend/app/agent/bootstrap.py"):
        assert "VllmModelAdapter(" not in Path(relative).read_text(encoding="utf-8")


def json_text(value: object) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True)
