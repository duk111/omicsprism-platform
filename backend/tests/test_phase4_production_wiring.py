from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone
from hashlib import sha256

import httpx
import pytest
from fastapi import HTTPException

from backend.app.agent import bootstrap
from backend.app.agent.graph import (
    AnalysisExecutionRequest,
    DatasetLoadRequest,
    MainModelOutput,
)
from backend.app.agent.model import VllmGraphModel
from backend.app.agent.context import DecisionLedger, FactIndex, MainModelContext, WorkingSet
from backend.app.agent.param_resolver import ContrastSpec, DEGParams
from backend.app.agent.product_store import (
    AgentResourceNotFound,
    InMemoryAgentProductStore,
)
from backend.app.agent.schemas import (
    AgentInputBundleRecord,
    AgentInputFileRecord,
    AgentThreadRecord,
)
from backend.app.models import FileArtifactKind, UploadedFileInfo
from backend.app.settings import AppSettings


COUNTS = b"gene,s1,s2,s3,s4\ng1,10,12,30,32\n"


class _Files:
    def __init__(self) -> None:
        self.payloads = {"agent-inputs/bundle-1/counts.csv": COUNTS}
        self.copies: list[tuple[str, str]] = []

    def open_storage_key(self, storage_key: str):
        return io.BytesIO(self.payloads[storage_key])

    def copy_staged_input(
        self, target_job_id: str, item: AgentInputFileRecord
    ) -> UploadedFileInfo:
        self.copies.append((target_job_id, item.file_id))
        return UploadedFileInfo(
            kind=FileArtifactKind.INPUT,
            field=item.field,
            filename=item.filename,
            path=f"inputs/{item.field}.csv",
            storage_key=f"jobs/{target_job_id}/inputs/{item.field}.csv",
            checksum=item.checksum,
            content_type=item.content_type,
            size_bytes=item.size_bytes,
            created_at=item.created_at,
        )


class _Jobs:
    def __init__(self) -> None:
        self.records = {}

    def get_for_user(self, job_id: str, user_id: str):
        record = self.records.get(job_id)
        if record is None or record.owner_id != user_id:
            raise HTTPException(status_code=404, detail="Job not found")
        return record

    def save(self, job) -> None:
        self.records[job.id] = job


class _Executor:
    def __init__(self) -> None:
        self.enqueued: list[str] = []

    def enqueue(self, job_id: str) -> None:
        self.enqueued.append(job_id)


def _product_store() -> InMemoryAgentProductStore:
    now = datetime.now(timezone.utc)
    store = InMemoryAgentProductStore()
    store.save_thread(AgentThreadRecord(
        thread_id="thread-1",
        user_id="user-1",
        title="production wiring",
        current_run_id="run-1",
        status="active",
        version=0,
        created_at=now,
        updated_at=now,
    ))
    store.save_input_bundle(AgentInputBundleRecord(
        bundle_id="bundle-1",
        thread_id="thread-1",
        user_id="user-1",
        status="active",
        expires_at=now + timedelta(hours=1),
        created_at=now,
    ))
    store.append_input_file(AgentInputFileRecord(
        file_id="file-1",
        bundle_id="bundle-1",
        user_id="user-1",
        field="counts",
        filename="counts.csv",
        storage_key="agent-inputs/bundle-1/counts.csv",
        checksum="sha256:" + sha256(COUNTS).hexdigest(),
        content_type="text/csv",
        size_bytes=len(COUNTS),
        created_at=now,
    ))
    return store


def _patch_stores(monkeypatch: pytest.MonkeyPatch, store) -> None:
    monkeypatch.setattr(bootstrap, "PostgresAgentProductStore", lambda _url: store)


def _settings(**changes) -> AppSettings:
    return AppSettings(
        storage_backend="postgres",
        runtime_database_url="postgresql://runtime",
        **changes,
    )


def test_graph_context_requires_model_and_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_stores(monkeypatch, _product_store())

    with pytest.raises(RuntimeError, match="AGENT_MODEL_URL"):
        bootstrap.create_agent_api_context(
            _settings(), files=_Files(), job_store=_Jobs()
        )

    with pytest.raises(RuntimeError, match="Job executor"):
        bootstrap.create_agent_api_context(
            _settings(
                agent_model_url="http://model-host:8000",
                agent_model_name="model",
            ),
            files=_Files(),
            job_store=_Jobs(),
        )


def test_context_builds_one_graph_with_owned_deterministic_adapters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _product_store()
    files = _Files()
    jobs = _Jobs()
    executor = _Executor()
    checkpointer = object()
    captured: list[tuple[object, ...]] = []
    graph = object()
    _patch_stores(monkeypatch, store)
    monkeypatch.setattr(bootstrap, "VllmGraphModel", lambda **_kwargs: object())
    monkeypatch.setattr(bootstrap, "_create_postgres_checkpointer", lambda _url: checkpointer)

    def build(*dependencies, **kwargs):
        captured.append((*dependencies, kwargs))
        return graph

    monkeypatch.setattr(bootstrap, "build_agent_graph", build)
    context = bootstrap.create_agent_api_context(
        _settings(
            agent_model_url="http://model-host:8000",
            agent_model_name="model",
        ),
        files=files,
        job_store=jobs,
        job_executor=executor,
    )

    assert context is not None
    assert context.graph is graph
    assert len(captured) == 1
    _, load_datasets, submit_job, _, _, kwargs = captured[0]
    assert kwargs["checkpointer"] is checkpointer
    refs = load_datasets(DatasetLoadRequest(
        user_id="user-1", dataset_ids=["file-1"]
    ))
    assert [(item.dataset_id, item.owner_id, item.content) for item in refs] == [
        ("file-1", "user-1", COUNTS)
    ]
    with pytest.raises(AgentResourceNotFound):
        load_datasets(DatasetLoadRequest(
            user_id="user-2", dataset_ids=["file-1"]
        ))
    files.payloads["agent-inputs/bundle-1/counts.csv"] = b"changed"
    with pytest.raises(ValueError, match="checksum changed"):
        load_datasets(DatasetLoadRequest(
            user_id="user-1", dataset_ids=["file-1"]
        ))
    files.payloads["agent-inputs/bundle-1/counts.csv"] = COUNTS

    request = AnalysisExecutionRequest(
        user_id="user-1",
        thread_id="thread-1",
        dataset_ids=["file-1"],
        resolved_params=DEGParams(contrast=ContrastSpec(
            compare_field="condition",
            tested_level="salt",
            reference_level="control",
        )),
        input_fingerprint="sha256:" + "1" * 64,
        idempotency_key="run-once",
    )
    first = submit_job(request)
    replay = submit_job(request)

    assert replay == first
    assert first.owner_id == "user-1"
    assert executor.enqueued == [first.job_id]
    assert files.copies == [(first.job_id, "file-1")]
    assert jobs.records[first.job_id].owner_id == "user-1"


def test_vllm_graph_model_uses_main_output_schema_and_returns_typed_output() -> None:
    captured: dict[str, object] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        output = {
            "decision": {
                "action": "ask_user",
                "question": "Which analysis should I run?",
            },
            "answer": None,
        }
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps(output)}}]
        })

    model = VllmGraphModel(
        base_url="http://model-host:8000",
        model="Qwen3",
        client=httpx.Client(transport=httpx.MockTransport(handle)),
    )
    context = MainModelContext(
        user_message="analyze my data",
        fact_index=FactIndex(context_version="facts.v1:test", dataset_roles=["counts"]),
        decision_ledger=DecisionLedger(context_version="ledger.v1:test"),
        working_set=WorkingSet(context_version="working.v1:test"),
    )

    result = model(context)

    assert isinstance(result, MainModelOutput)
    assert result.decision.action == "ask_user"
    assert captured["url"] == "http://model-host:8000/v1/chat/completions"
    body = captured["body"]
    assert body["response_format"]["json_schema"]["schema"] == (
        MainModelOutput.model_json_schema()
    )
    assert json.loads(body["messages"][1]["content"]) == context.model_dump(
        mode="json"
    )


def test_main_output_schema_requires_answer_for_answer_action() -> None:
    schema = MainModelOutput.model_json_schema()

    assert len(schema["oneOf"]) == 2
    answer_branch, non_answer_branch = schema["oneOf"]
    assert answer_branch["required"] == ["decision", "answer"]
    assert non_answer_branch["required"] == ["decision", "answer"]
    assert answer_branch["properties"]["answer"]["type"] == "string"
    assert non_answer_branch["properties"]["answer"]["type"] == "null"
    assert answer_branch["properties"]["decision"]["allOf"][1]["properties"]["action"]["const"] == "answer"


def test_vllm_graph_model_ignores_spurious_tool_fields_on_answer() -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        output = {
            "decision": {
                "action": "answer",
                "arguments": {"question": "wrong branch"},
            },
            "answer": "A direct answer.",
        }
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps(output)}}]
        })

    model = VllmGraphModel(
        base_url="http://model-host:8000/v1",
        model="Qwen3",
        client=httpx.Client(transport=httpx.MockTransport(handle)),
    )
    context = MainModelContext(
        user_message="hello",
        fact_index=FactIndex(context_version="facts.v1:test"),
        decision_ledger=DecisionLedger(context_version="ledger.v1:test"),
        working_set=WorkingSet(context_version="working.v1:test"),
    )

    result = model(context)

    assert result.decision.action == "answer"
    assert result.answer == "A direct answer."


def test_vllm_graph_model_ignores_spurious_action_fields() -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        output = {
            "decision": {
                "action": "answer",
                "result_query": {
                    "artifact": "wrong-branch.csv",
                    "filters": {},
                    "limit": 1,
                },
                "grounded_answer": {
                    "claims": [],
                },
                "job_id": "wrong-job",
                "question": "wrong question",
                "proposal": {"analysis_type": "DEG"},
            },
            "answer": "A direct answer.",
        }
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps(output)}}]
        })

    model = VllmGraphModel(
        base_url="http://model-host:8000/v1",
        model="Qwen3",
        client=httpx.Client(transport=httpx.MockTransport(handle)),
    )
    context = MainModelContext(
        user_message="hello",
        fact_index=FactIndex(context_version="facts.v1:test"),
        decision_ledger=DecisionLedger(context_version="ledger.v1:test"),
        working_set=WorkingSet(context_version="working.v1:test"),
    )

    result = model(context)

    assert result.decision.action == "answer"
    assert result.decision.result_query is None
    assert result.decision.grounded_answer is None
    assert result.decision.job_id is None
    assert result.decision.question is None
    assert result.decision.proposal is None
