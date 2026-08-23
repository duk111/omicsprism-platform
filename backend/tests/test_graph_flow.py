from __future__ import annotations

from hashlib import sha256

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from backend.app.agent.dataset_profile import build_dataset_profiles
from backend.app.agent.graph import (
    AnalysisExecutionRequest,
    AgentDecision,
    DatasetLoadRequest,
    DatasetProfileRef,
    GraphState,
    JobLookupRequest,
    JobRef,
    JobSummary,
    MainModelContext,
    MainModelOutput,
    ResultEvidenceRequest,
    ResultQuerySpec,
    StepBudget,
    build_agent_graph,
)
from backend.app.agent.nodes.analysis import DatasetLoadError
from backend.app.agent.nodes.result_qa import ResultAccessError
from backend.app.agent.param_resolver import AnalysisProposal
from backend.app.agent.schemas import ToolName, ToolResult
from backend.app.agent.validation import DatasetRef


COUNTS = b"gene,s1,s2,s3,s4,s5,s6\ng1,10,12,30,32,20,22\n"
METADATA = (
    b"sample_id,condition\n"
    b"s1,control\ns2,control\ns3,salt\ns4,salt\ns5,drought\ns6,drought\n"
)


class ScriptedMainModel:
    def __init__(self, outputs: list[object]) -> None:
        self.outputs = list(outputs)
        self.contexts: list[MainModelContext] = []

    def __call__(self, context: MainModelContext) -> object:
        self.contexts.append(context)
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return output


class RecordingDatasetLoader:
    def __init__(self, refs: list[DatasetRef]) -> None:
        self.refs = refs
        self.requests: list[DatasetLoadRequest] = []

    def __call__(self, request: DatasetLoadRequest) -> list[DatasetRef]:
        self.requests.append(request)
        return [item.model_copy(deep=True) for item in self.refs]


class RecordingJobSubmitter:
    def __init__(self) -> None:
        self.requests: list[AnalysisExecutionRequest] = []

    def __call__(self, request: AnalysisExecutionRequest) -> JobRef:
        self.requests.append(request)
        return JobRef(job_id=f"job-{len(self.requests)}", owner_id=request.user_id)


class RecordingJobReader:
    def __init__(self, summaries: list[JobSummary] | None = None) -> None:
        self.summaries = {item.job_id: item for item in summaries or []}
        self.requests: list[JobLookupRequest] = []

    def __call__(self, request: JobLookupRequest) -> JobSummary:
        self.requests.append(request)
        try:
            return self.summaries[request.job_id].model_copy(deep=True)
        except KeyError as exc:
            raise LookupError(request.job_id) from exc


class RecordingResultQuerier:
    def __init__(self, evidence: ToolResult | None = None) -> None:
        self.evidence = evidence
        self.requests: list[ResultEvidenceRequest] = []

    def __call__(self, request: ResultEvidenceRequest) -> ToolResult:
        self.requests.append(request)
        if self.evidence is None:
            raise LookupError(request.query.artifact)
        return self.evidence.model_copy(deep=True)


def _dataset_refs(
    *,
    counts: bytes = COUNTS,
    metadata: bytes = METADATA,
    owner_id: str = "user-1",
) -> list[DatasetRef]:
    inputs = {
        "counts": ("counts.csv", counts),
        "metadata": ("metadata.csv", metadata),
    }
    profiles = {item.role: item for item in build_dataset_profiles(inputs)}
    return [
        DatasetRef(
            dataset_id=f"dataset-{role}",
            owner_id=owner_id,
            role=role,
            filename=filename,
            checksum="sha256:" + sha256(content).hexdigest(),
            content=content,
            profile=profiles[role],
        )
        for role, (filename, content) in inputs.items()
    ]


def _profile_refs(refs: list[DatasetRef]) -> list[DatasetProfileRef]:
    return [
        DatasetProfileRef(
            dataset_id=item.dataset_id,
            owner_id=item.owner_id,
            filename=item.filename,
            checksum=item.checksum,
            profile=item.profile,
        )
        for item in refs
        if item.profile is not None
    ]


def _state(**overrides: object) -> GraphState:
    values: dict[str, object] = {
        "thread_id": "thread-1",
        "user_id": "user-1",
        "user_message": "What is differential expression?",
    }
    values.update(overrides)
    return GraphState.model_validate(values)


def _submitter() -> RecordingJobSubmitter:
    return RecordingJobSubmitter()


def _reader(*summaries: JobSummary) -> RecordingJobReader:
    return RecordingJobReader(list(summaries))


def _querier(evidence: ToolResult | None = None) -> RecordingResultQuerier:
    return RecordingResultQuerier(evidence)


def _run(model: ScriptedMainModel, state: GraphState | None = None) -> GraphState:
    result = build_agent_graph(
        model,
        lambda _request: [],
        _submitter(),
        _reader(),
        _querier(),
    ).invoke(state or _state())
    return GraphState.model_validate(result)


def test_general_knowledge_routes_to_direct_answer() -> None:
    model = ScriptedMainModel([MainModelOutput(
        decision=AgentDecision(action="answer", decision_note="general knowledge"),
        answer="Differential expression compares feature abundance between conditions.",
    )])

    result = _run(model)

    assert result.decision is not None
    assert result.decision.action == "answer"
    assert result.response_text.startswith("Differential expression")
    assert result.step_budget.used_steps == 1


def test_insufficient_intent_routes_to_ask_user() -> None:
    model = ScriptedMainModel([{
        "decision": {"action": "ask_user", "question": "What would you like to do?"},
        "answer": None,
    }])

    result = _run(model, _state(user_message="Help"))

    assert result.decision is not None
    assert result.decision.action == "ask_user"
    assert result.response_text == "What would you like to do?"


def test_schema_failure_retries_once_then_asks_user() -> None:
    model = ScriptedMainModel([
        {"decision": {"action": "not-valid"}},
        {"decision": {"action": "still-invalid"}},
    ])

    result = _run(model)

    assert len(model.contexts) == 2
    assert result.decision is not None
    assert result.decision.action == "ask_user"
    assert "无法可靠判断" in (result.response_text or "")


def test_model_error_can_recover_on_the_single_retry() -> None:
    model = ScriptedMainModel([
        RuntimeError("model unavailable"),
        MainModelOutput(
            decision=AgentDecision(action="answer"),
            answer="Recovered answer.",
        ),
    ])

    result = _run(model)

    assert len(model.contexts) == 2
    assert result.response_text == "Recovered answer."


def test_result_qa_queries_evidence_and_returns_verified_citations() -> None:
    artifact = "differential_gene_counts.csv"
    summary = JobSummary(
        job_id="job-7",
        owner_id="user-1",
        status="succeeded",
        progress=100,
        artifacts=[artifact],
    )
    evidence = ToolResult(
        tool=ToolName.QUERY_RESULT_EVIDENCE,
        ok=True,
        rows=[{
            "_row_id": 4,
            "Gene": "GeneA",
            "log2FoldChange": "2.5",
            "padj": "0.01",
        }],
        truncated=False,
        row_count=1,
        artifact=artifact,
        checksum="sha256:fixture",
        filters={},
        sort=None,
        error_code=None,
    )
    model = ScriptedMainModel([MainModelOutput(
        decision=AgentDecision(
            action="query_result",
            job_id="job-7",
            result_query=ResultQuerySpec(artifact=artifact, resolve_entity="GeneA"),
        ),
    )])
    reader = _reader(summary)
    querier = _querier(evidence)
    submitter = _submitter()

    result = GraphState.model_validate(build_agent_graph(
        model, lambda _request: [], submitter, reader, querier
    ).invoke(_state(user_message="What happened to GeneA?")))

    assert result.current_job == JobRef(job_id="job-7", owner_id="user-1")
    assert result.job_summary == summary
    assert result.grounded_answer is not None
    claim = result.grounded_answer.claims[0]
    assert "2.5" in claim.text
    assert claim.citation.artifact == artifact
    assert claim.citation.checksum == "sha256:fixture"
    assert claim.citation.row_ids == [4]
    assert reader.requests == [JobLookupRequest(user_id="user-1", job_id="job-7")]
    assert querier.requests == [ResultEvidenceRequest(
        user_id="user-1",
        job_id="job-7",
        query=ResultQuerySpec(artifact=artifact, resolve_entity="GeneA"),
    )]
    assert not submitter.requests


def test_get_job_uses_current_job_and_returns_compact_summary() -> None:
    summary = JobSummary(
        job_id="job-current",
        owner_id="user-1",
        status="running",
        progress=40,
        progress_step="Differential analysis",
        artifacts=[],
    )
    reader = _reader(summary)
    querier = _querier()
    model = ScriptedMainModel([MainModelOutput(
        decision=AgentDecision(action="get_job"),
    )])

    result = GraphState.model_validate(build_agent_graph(
        model, lambda _request: [], _submitter(), reader, querier
    ).invoke(_state(current_job=JobRef(job_id="job-current", owner_id="user-1"))))

    assert result.job_summary == summary
    assert result.grounded_answer is None
    assert result.response_text == "Job job-current: running (40%) - Differential analysis"
    assert reader.requests == [JobLookupRequest(user_id="user-1", job_id="job-current")]
    assert not querier.requests


def test_result_qa_uses_the_only_recent_job_but_does_not_guess_among_several() -> None:
    only = JobSummary(
        job_id="job-only", owner_id="user-1", status="succeeded"
    )
    single_reader = _reader(only)
    model = ScriptedMainModel([MainModelOutput(
        decision=AgentDecision(action="get_job"),
    )])
    single = GraphState.model_validate(build_agent_graph(
        model, lambda _request: [], _submitter(), single_reader, _querier()
    ).invoke(_state(recent_jobs=[JobRef(job_id="job-only", owner_id="user-1")])))

    assert single.job_summary == only

    ambiguous_reader = _reader()
    ambiguous = GraphState.model_validate(build_agent_graph(
        ScriptedMainModel([MainModelOutput(
            decision=AgentDecision(action="get_job"),
        )]),
        lambda _request: [],
        _submitter(),
        ambiguous_reader,
        _querier(),
    ).invoke(_state(recent_jobs=[
        JobRef(job_id="job-1", owner_id="user-1"),
        JobRef(job_id="job-2", owner_id="user-1"),
    ])))

    assert ambiguous.job_summary is None
    assert ambiguous.response_text == "Specify which Job to use: job-1, job-2"
    assert not ambiguous_reader.requests


def test_result_qa_rejects_cross_user_job_reader_response() -> None:
    reader = _reader(JobSummary(
        job_id="job-7", owner_id="user-2", status="succeeded"
    ))
    model = ScriptedMainModel([MainModelOutput(
        decision=AgentDecision(action="get_job", job_id="job-7"),
    )])

    with pytest.raises(ResultAccessError, match="cross-user"):
        build_agent_graph(
            model, lambda _request: [], _submitter(), reader, _querier()
        ).invoke(_state())


def test_graph_has_only_three_semantic_nodes() -> None:
    graph = build_agent_graph(
        ScriptedMainModel([]),
        lambda _request: [],
        _submitter(),
        _reader(),
        _querier(),
    ).get_graph()

    assert set(graph.nodes) == {"__start__", "main", "analysis", "result_qa", "__end__"}


def test_main_model_context_excludes_owner_and_dataset_payloads() -> None:
    model = ScriptedMainModel([MainModelOutput(
        decision=AgentDecision(action="answer"),
        answer="A bounded answer.",
    )])

    _run(model)

    payload = model.contexts[0].model_dump()
    assert set(payload) == {
        "user_message",
        "conversation_summary",
        "dataset_roles",
        "current_job_id",
        "recent_job_ids",
    }
    assert "user_id" not in payload
    assert "owner_id" not in payload


def test_exhausted_step_budget_does_not_call_model() -> None:
    model = ScriptedMainModel([])

    result = _run(model, _state(step_budget=StepBudget(max_steps=1, used_steps=1)))

    assert model.contexts == []
    assert result.decision is not None
    assert result.decision.action == "ask_user"
    assert result.step_budget.used_steps == 1


def _analysis_model(proposal: AnalysisProposal) -> ScriptedMainModel:
    return ScriptedMainModel([MainModelOutput(
        decision=AgentDecision(
            action="run_analysis",
            analysis_type="DEG",
            proposal=proposal,
        ),
    )])


def test_complete_analysis_request_is_resolved_and_validated_before_confirmation() -> None:
    refs = _dataset_refs()
    loader = RecordingDatasetLoader(refs)
    submitter = _submitter()
    model = _analysis_model(AnalysisProposal(
        analysis_type="DEG",
        compare_field="condition",
        tested_level="salt",
        reference_level="control",
    ))
    state = _state(
        user_message="Compare salt and control",
        dataset_profiles=_profile_refs(refs),
    )

    result = build_agent_graph(
        model, loader, submitter, _reader(), _querier()
    ).invoke(state, {
        "configurable": {"thread_id": "confirmation-ready"},
    })

    assert result["__interrupt__"][0].value["kind"] == "confirmation"
    payload = result["__interrupt__"][0].value
    assert payload["analysis_type"] == "DEG"
    assert payload["resolved_params"]["contrast"]["tested_level"] == "salt"
    assert payload["preview"]["tested_count"] == 2
    assert not submitter.requests
    assert loader.requests == [DatasetLoadRequest(
        user_id="user-1",
        dataset_ids=["dataset-counts", "dataset-metadata"],
    )]


def test_ambiguity_interrupt_resume_re_resolves_and_revalidates() -> None:
    refs = _dataset_refs()
    loader = RecordingDatasetLoader(refs)
    submitter = _submitter()
    graph = build_agent_graph(
        _analysis_model(AnalysisProposal(analysis_type="DEG", compare_field="condition")),
        loader,
        submitter,
        _reader(),
        _querier(),
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "clarification-flow"}}
    state = _state(
        user_message="Analyze treatment response",
        dataset_profiles=_profile_refs(refs),
    )

    paused = graph.invoke(state, config)

    assert paused["__interrupt__"][0].value["kind"] == "clarification"
    assert paused["__interrupt__"][0].value["missing"][0]["field"] == "tested_level"

    resumed = graph.invoke(
        Command(resume={"answer": "compare salt and control"}), config
    )

    assert resumed["__interrupt__"][0].value["kind"] == "confirmation"
    assert resumed["clarification_answer"] == "compare salt and control"
    assert resumed["resolved_request"].params is not None
    assert resumed["resolved_request"].params.contrast.tested_level == "salt"
    assert resumed["validation_report"].ok
    assert resumed["pending_interrupt"].kind == "confirmation"
    assert len(loader.requests) == 3
    assert not submitter.requests


def test_confirmation_run_submits_validated_params_once() -> None:
    refs = _dataset_refs()
    loader = RecordingDatasetLoader(refs)
    submitter = _submitter()
    graph = build_agent_graph(
        _analysis_model(AnalysisProposal(
            analysis_type="DEG",
            compare_field="condition",
            tested_level="salt",
            reference_level="control",
        )),
        loader,
        submitter,
        _reader(),
        _querier(),
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "confirmation-run"}}
    graph.invoke(
        _state(user_message="Run DEG", dataset_profiles=_profile_refs(refs)),
        config,
    )

    completed = GraphState.model_validate(graph.invoke(Command(resume={
        "action": "run",
        "idempotency_key": "run-1",
    }), config))

    assert completed.current_job == JobRef(job_id="job-1", owner_id="user-1")
    assert completed.recent_jobs == [completed.current_job]
    assert completed.pending_interrupt is None
    assert len(submitter.requests) == 1
    request = submitter.requests[0]
    assert request.user_id == "user-1"
    assert request.dataset_ids == ["dataset-counts", "dataset-metadata"]
    assert request.resolved_params.contrast.tested_level == "salt"
    assert request.idempotency_key == "run-1"
    assert len(loader.requests) == 2

    repeated = GraphState.model_validate(graph.invoke(Command(resume={
        "action": "run",
        "idempotency_key": "run-1",
    }), config))
    assert repeated.current_job == completed.current_job
    assert len(submitter.requests) == 1


def test_confirmation_modify_re_resolves_and_revalidates() -> None:
    refs = _dataset_refs()
    loader = RecordingDatasetLoader(refs)
    submitter = _submitter()
    graph = build_agent_graph(
        _analysis_model(AnalysisProposal(
            analysis_type="DEG",
            compare_field="condition",
            tested_level="salt",
            reference_level="control",
        )),
        loader,
        submitter,
        _reader(),
        _querier(),
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "confirmation-modify"}}
    graph.invoke(
        _state(user_message="Run DEG", dataset_profiles=_profile_refs(refs)),
        config,
    )

    modified = graph.invoke(Command(resume={
        "action": "modify",
        "modification": "compare drought and control",
    }), config)

    payload = modified["__interrupt__"][0].value
    assert payload["kind"] == "confirmation"
    assert payload["resolved_params"]["contrast"]["tested_level"] == "drought"
    assert modified["validation_report"].ok
    assert len(loader.requests) == 2
    assert not submitter.requests


def test_confirmation_cancel_does_not_create_a_job() -> None:
    refs = _dataset_refs()
    loader = RecordingDatasetLoader(refs)
    submitter = _submitter()
    graph = build_agent_graph(
        _analysis_model(AnalysisProposal(
            analysis_type="DEG",
            compare_field="condition",
            tested_level="salt",
            reference_level="control",
        )),
        loader,
        submitter,
        _reader(),
        _querier(),
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "confirmation-cancel"}}
    graph.invoke(
        _state(user_message="Run DEG", dataset_profiles=_profile_refs(refs)),
        config,
    )

    cancelled = GraphState.model_validate(
        graph.invoke(Command(resume={"action": "cancel"}), config)
    )

    assert cancelled.current_job is None
    assert cancelled.pending_interrupt is None
    assert cancelled.response_text == "Analysis cancelled."
    assert len(loader.requests) == 1
    assert not submitter.requests


def test_changed_input_rejects_execution_and_asks_for_clarification() -> None:
    refs = _dataset_refs()
    loader = RecordingDatasetLoader(refs)
    submitter = _submitter()
    graph = build_agent_graph(
        _analysis_model(AnalysisProposal(
            analysis_type="DEG",
            compare_field="condition",
            tested_level="salt",
            reference_level="control",
        )),
        loader,
        submitter,
        _reader(),
        _querier(),
        checkpointer=InMemorySaver(),
    )
    config = {"configurable": {"thread_id": "changed-input"}}
    graph.invoke(
        _state(user_message="Run DEG", dataset_profiles=_profile_refs(refs)),
        config,
    )
    changed_counts = COUNTS.replace(b"g1,10", b"g1,11")
    loader.refs[0] = loader.refs[0].model_copy(update={
        "content": changed_counts,
        "checksum": "sha256:" + sha256(changed_counts).hexdigest(),
    })

    rejected = graph.invoke(Command(resume={
        "action": "run",
        "idempotency_key": "changed-run",
    }), config)

    payload = rejected["__interrupt__"][0].value
    assert payload["kind"] == "clarification"
    assert payload["missing"][0]["field"] == "input_fingerprint"
    assert rejected.get("current_job") is None
    assert not submitter.requests


def test_blocking_validation_interrupts_without_creating_a_job() -> None:
    negative_counts = b"gene,s1,s2,s3,s4,s5,s6\ng1,10,-1,30,32,20,22\n"
    refs = _dataset_refs(counts=negative_counts)
    loader = RecordingDatasetLoader(refs)
    submitter = _submitter()
    graph = build_agent_graph(
        _analysis_model(AnalysisProposal(
            analysis_type="DEG",
            compare_field="condition",
            tested_level="salt",
            reference_level="control",
        )),
        loader,
        submitter,
        _reader(),
        _querier(),
        checkpointer=InMemorySaver(),
    )

    paused = graph.invoke(
        _state(user_message="Run DEG", dataset_profiles=_profile_refs(refs)),
        {"configurable": {"thread_id": "blocking-validation"}},
    )

    payload = paused["__interrupt__"][0].value
    assert payload["kind"] == "clarification"
    assert any(item["field"] == "counts" for item in payload["missing"])
    assert paused.get("current_job") is None


def test_analysis_loader_rejects_cross_user_dataset() -> None:
    state_refs = _dataset_refs()
    loader = RecordingDatasetLoader(_dataset_refs(owner_id="user-2"))
    submitter = _submitter()
    graph = build_agent_graph(
        _analysis_model(AnalysisProposal(
            analysis_type="DEG",
            compare_field="condition",
            tested_level="salt",
            reference_level="control",
        )),
        loader,
        submitter,
        _reader(),
        _querier(),
    )

    with pytest.raises(DatasetLoadError, match="cross-user"):
        graph.invoke(_state(
            user_message="Run DEG",
            dataset_profiles=_profile_refs(state_refs),
        ))
