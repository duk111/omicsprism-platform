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
    AgentRole,
    AnalysisModelOutput,
    AnalysisDecision,
    DatasetLoadRequest,
    DatasetProfileRef,
    GraphState,
    QaModelOutput,
    QaDecision,
    ResultQaModelOutput,
    ResultDecision,
    ToolCallRequest,
)
from backend.app.agent.dataset_profile import build_dataset_profiles
from backend.app.agent.model import VllmGraphModel
from backend.app.agent.capabilities import readonly_openai_tool_definitions
from backend.app.agent.context import (
    AnalysisModelContext,
    DecisionLedger,
    FactIndex,
    QaFactIndex,
    QaModelContext,
    RecentMessages,
    ResultQaModelContext,
    ResultFocusContext,
    JobContextRef,
    ToolObservationContext,
)
from backend.app.agent.param_resolver import ContrastSpec, DEGParams
from backend.app.agent.product_store import (
    AgentResourceNotFound,
    InMemoryAgentProductStore,
)
from backend.app.agent.schemas import (
    AgentInputBundleRecord,
    AgentInputFileRecord,
    AgentThreadRecord,
    ToolName,
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


def test_production_tool_executor_loads_owned_inputs_and_dispatches_read_only_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata = b"sample_id,condition\ns1,salt\ns2,salt\ns3,control\ns4,control\n"
    store = _product_store()
    store.append_input_file(AgentInputFileRecord(
        file_id="file-2",
        bundle_id="bundle-1",
        user_id="user-1",
        field="metadata",
        filename="metadata.csv",
        storage_key="agent-inputs/bundle-1/metadata.csv",
        checksum="sha256:" + sha256(metadata).hexdigest(),
        content_type="text/csv",
        size_bytes=len(metadata),
        created_at=datetime.now(timezone.utc),
    ))
    files = _Files()
    files.payloads["agent-inputs/bundle-1/metadata.csv"] = metadata
    captured: list[dict[str, object]] = []
    _patch_stores(monkeypatch, store)
    monkeypatch.setattr(bootstrap, "VllmGraphModel", lambda **_kwargs: object())
    monkeypatch.setattr(bootstrap, "_create_postgres_checkpointer", lambda _url: object())
    monkeypatch.setattr(
        bootstrap,
        "build_agent_graph",
        lambda *_args, **kwargs: captured.append(kwargs) or object(),
    )

    context = bootstrap.create_agent_api_context(
        _settings(
            agent_model_url="http://model-host:8000",
            agent_model_name="model",
        ),
        files=files,
        job_store=_Jobs(),
        job_executor=_Executor(),
    )

    assert context is not None
    executor = captured[0]["tool_executor"]
    assert callable(executor)
    profiles = {
        item.role: item
        for item in build_dataset_profiles({
            "counts": ("counts.csv", COUNTS),
            "metadata": ("metadata.csv", metadata),
        })
    }
    refs = [
        DatasetProfileRef(
            dataset_id=file_id,
            owner_id="user-1",
            filename=filename,
            checksum=checksum,
            profile=profiles[role],
        )
        for file_id, role, filename, checksum in [
            ("file-1", "counts", "counts.csv", "sha256:" + sha256(COUNTS).hexdigest()),
            ("file-2", "metadata", "metadata.csv", "sha256:" + sha256(metadata).hexdigest()),
        ]
    ]
    state = GraphState(
        thread_id="thread-1",
        user_id="user-1",
        user_message="inspect metadata",
        dataset_profiles=refs,
    )
    result = executor(
        ToolCallRequest(tool=ToolName.DESCRIBE_METADATA, arguments={}),
        state,
    )
    assert result.ok is True
    assert [field.field for field in result.fields] == ["condition"]

    foreign_state = state.model_copy(update={"user_id": "user-2"})
    with pytest.raises(HTTPException) as exc_info:
        executor(
            ToolCallRequest(tool=ToolName.DESCRIBE_METADATA, arguments={}),
            foreign_state,
        )
    assert exc_info.value.status_code == 404


def test_job_submission_persists_an_ownership_bound_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _product_store()
    files = _Files()
    jobs = _Jobs()
    executor = _Executor()
    _patch_stores(monkeypatch, store)
    monkeypatch.setattr(bootstrap, "VllmGraphModel", lambda **_kwargs: object())
    monkeypatch.setattr(bootstrap, "_create_postgres_checkpointer", lambda _url: object())
    captured: list[object] = []
    monkeypatch.setattr(
        bootstrap,
        "build_agent_graph",
        lambda *args, **_kwargs: captured.append(args[2]) or object(),
    )
    context = bootstrap.create_agent_api_context(
        _settings(
            agent_model_url="http://model-host:8000",
            agent_model_name="model",
        ),
        files=files,
        job_store=jobs,
        job_executor=executor,
    )
    submit = captured[0]
    request = AnalysisExecutionRequest(
        user_id="user-1",
        thread_id="thread-1",
        turn_id="turn-1",
        run_id="run-1",
        trace_id="trace-1",
        dataset_ids=["file-1"],
        resolved_params=DEGParams(contrast=ContrastSpec(
            compare_field="condition",
            tested_level="salt",
            reference_level="control",
        )),
        input_fingerprint="sha256:" + "1" * 64,
        idempotency_key="wait-once",
    )
    job = submit(request)
    wait = store.get_job_wait(job_id=job.job_id, user_id="user-1")
    assert (wait.thread_id, wait.turn_id, wait.run_id, wait.trace_id) == (
        "thread-1", "turn-1", "run-1", "trace-1"
    )


@pytest.mark.parametrize(
    ("role", "context", "output", "expected_tools", "schema_name"),
    [
        (
            AgentRole.QA,
            QaModelContext(
                user_message="What data roles are available?",
                recent_messages=RecentMessages(context_version="messages.v1:test"),
                fact_index=QaFactIndex(dataset_roles=["metadata", "counts"]),
            ),
            QaModelOutput(
                decision=QaDecision(action="answer"),
                answer="Metadata and counts are available.",
            ),
            set(),
            "qa_model_output",
        ),
        (
            AgentRole.ANALYSIS,
            AnalysisModelContext(
                user_message="Which analyses can these data support?",
                fact_index=FactIndex(
                    context_version="facts.v1:test",
                    dataset_roles=["metadata", "counts"],
                    metadata_fields=["treatment"],
                    metadata_levels={"treatment": {"control": 2, "salt": 2}},
                ),
                decision_ledger=DecisionLedger(context_version="ledger.v1:test"),
            ),
            AnalysisModelOutput(
                decision=AnalysisDecision(action="propose_plan"),
            ),
            {"describe_metadata", "enumerate_contrasts"},
            "analysis_model_output",
        ),
        (
            AgentRole.RESULT_QA,
            ResultQaModelContext(
                user_message="What happened to job-1?",
                current_job=JobContextRef(job_id="job-1", owner_id="user-1"),
                focus=ResultFocusContext(in_scope_job_ids=["job-1"]),
                job_artifacts={"job-1": ["result.csv"]},
            ),
            ResultQaModelOutput(
                decision=ResultDecision(action="reroute", reroute_to="qa"),
            ),
            {"list_jobs", "describe_artifacts", "query_artifact"},
            "result_qa_model_output",
        ),
    ],
)
def test_vllm_graph_model_uses_role_specific_prompt_tools_and_schema(
    role,
    context,
    output,
    expected_tools,
    schema_name,
) -> None:
    captured: dict[str, object] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps(output.model_dump(mode="json"))}}]
        })

    model = VllmGraphModel(
        base_url="http://model-host:8000",
        model="Qwen3",
        client=httpx.Client(transport=httpx.MockTransport(handle)),
    )

    result = model(context, role=role)
    body = captured["body"]
    assert isinstance(body, dict)
    assert type(result) is type(output)
    assert body["response_format"]["json_schema"]["name"] == schema_name
    assert body["response_format"]["json_schema"]["schema"] == type(output).model_json_schema()
    schema = body["response_format"]["json_schema"]["schema"]
    assert "oneOf" not in json.dumps(schema)
    assert "allOf" not in json.dumps(schema)
    tool_names = {
        item["function"]["name"]
        for item in body["tools"]
    }
    assert tool_names == expected_tools
    assert body["messages"][0]["content"]
    assert json.loads(body["messages"][1]["content"]) == context.model_dump(mode="json")


def test_vllm_graph_model_emits_native_tool_calls_and_replays_tool_result() -> None:
    requests: list[dict[str, object]] = []
    call_id = "call-native-1"

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            output = {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": "describe_metadata",
                        "arguments": '{"fields":["treatment"]}',
                    },
                }],
            }
        else:
            output = {
                "role": "assistant",
                "content": "The treatment field is categorical.",
                "tool_calls": [],
            }
        return httpx.Response(200, json={"choices": [{"message": output}]})

    model = VllmGraphModel(
        base_url="http://model-host:8000",
        model="Qwen3",
        client=httpx.Client(transport=httpx.MockTransport(handle)),
        structured_tool_response=False,
    )
    context = AnalysisModelContext(
        thread_id="thread-native",
        turn_id="turn-native",
        run_id="run-native",
        user_message="Inspect treatment",
        fact_index=FactIndex(context_version="facts.v1:test"),
        decision_ledger=DecisionLedger(context_version="ledger.v1:test"),
    )

    first = model(context, role=AgentRole.ANALYSIS)
    assert first.decision.action == "tool_call"
    assert model.last_tool_calls[0]["id"] == call_id
    observation = ToolObservationContext(
        tool="describe_metadata",
        call_id=call_id,
        arguments={"fields": ["treatment"]},
        summary='{"ok":true,"fields":[]}',
    )
    second = model(
        context.model_copy(update={"tool_observations": [observation]}),
        role=AgentRole.ANALYSIS,
    )

    assert second.decision.action == "answer"
    assert requests[0]["tools"] == readonly_openai_tool_definitions(
        names={"describe_metadata", "enumerate_contrasts"}
    )
    messages = requests[1]["messages"]
    assert messages[-2]["role"] == "assistant"
    assert messages[-2]["tool_calls"][0]["id"] == call_id
    assert messages[-1] == {
        "role": "tool",
        "tool_call_id": call_id,
        "content": '{"ok":true,"fields":[]}',
    }


def test_vllm_graph_model_accepts_native_tool_calls_for_analysis_role() -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "analysis-call-1",
                "type": "function",
                "function": {
                    "name": "describe_metadata",
                    "arguments": '{"fields":["treatment"]}',
                },
            }],
        }}]})

    model = VllmGraphModel(
        base_url="http://model-host:8000",
        model="Qwen3",
        client=httpx.Client(transport=httpx.MockTransport(handle)),
        structured_tool_response=False,
    )
    result = model(AnalysisModelContext(
        user_message="Which analyses can these data support?",
        fact_index=FactIndex(context_version="facts.v1:test"),
        decision_ledger=DecisionLedger(context_version="ledger.v1:test"),
    ), role=AgentRole.ANALYSIS)

    assert isinstance(result, AnalysisModelOutput)
    assert result.decision.action == "tool_call"
    assert result.decision.tool is ToolName.DESCRIBE_METADATA
