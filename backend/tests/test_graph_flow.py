from __future__ import annotations

from hashlib import sha256

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from backend.app.agent.dataset_profile import build_dataset_profiles
from backend.app.agent.graph import (
    AgentDecision,
    DatasetLoadRequest,
    DatasetProfileRef,
    GraphState,
    MainModelContext,
    MainModelOutput,
    StepBudget,
    build_agent_graph,
)
from backend.app.agent.nodes.analysis import DatasetLoadError
from backend.app.agent.param_resolver import AnalysisProposal
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


def _run(model: ScriptedMainModel, state: GraphState | None = None) -> GraphState:
    result = build_agent_graph(model, lambda _request: []).invoke(state or _state())
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


def test_result_qa_route_is_still_a_placeholder_without_side_effects() -> None:
    model = ScriptedMainModel([MainModelOutput(
        decision=AgentDecision(action="query_result"),
    )])

    result = _run(model)

    assert result.decision is not None
    assert result.decision.action == "query_result"
    assert result.response_text is None
    assert result.current_job is None


def test_graph_has_only_three_semantic_nodes() -> None:
    graph = build_agent_graph(ScriptedMainModel([]), lambda _request: []).get_graph()

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

    result = GraphState.model_validate(build_agent_graph(model, loader).invoke(state))

    assert result.resolved_request is not None
    assert result.resolved_request.params is not None
    assert result.validation_report is not None
    assert result.validation_report.ok
    assert result.validation_report.preview is not None
    assert result.pending_interrupt is None
    assert result.current_job is None
    assert loader.requests == [DatasetLoadRequest(
        user_id="user-1",
        dataset_ids=["dataset-counts", "dataset-metadata"],
    )]


def test_ambiguity_interrupt_resume_re_resolves_and_revalidates() -> None:
    refs = _dataset_refs()
    loader = RecordingDatasetLoader(refs)
    graph = build_agent_graph(
        _analysis_model(AnalysisProposal(analysis_type="DEG", compare_field="condition")),
        loader,
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

    resumed = GraphState.model_validate(
        graph.invoke(Command(resume={"answer": "compare salt and control"}), config)
    )

    assert resumed.clarification_answer == "compare salt and control"
    assert resumed.resolved_request is not None
    assert resumed.resolved_request.params is not None
    assert resumed.resolved_request.params.contrast.tested_level == "salt"
    assert resumed.validation_report is not None
    assert resumed.validation_report.ok
    assert resumed.pending_interrupt is None
    assert len(loader.requests) == 3


def test_blocking_validation_interrupts_without_creating_a_job() -> None:
    negative_counts = b"gene,s1,s2,s3,s4,s5,s6\ng1,10,-1,30,32,20,22\n"
    refs = _dataset_refs(counts=negative_counts)
    loader = RecordingDatasetLoader(refs)
    graph = build_agent_graph(
        _analysis_model(AnalysisProposal(
            analysis_type="DEG",
            compare_field="condition",
            tested_level="salt",
            reference_level="control",
        )),
        loader,
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
    graph = build_agent_graph(
        _analysis_model(AnalysisProposal(
            analysis_type="DEG",
            compare_field="condition",
            tested_level="salt",
            reference_level="control",
        )),
        loader,
    )

    with pytest.raises(DatasetLoadError, match="cross-user"):
        graph.invoke(_state(
            user_message="Run DEG",
            dataset_profiles=_profile_refs(state_refs),
        ))
