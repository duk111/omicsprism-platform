from __future__ import annotations

import pytest

from backend.app.agent.graph import (
    AgentDecision,
    GraphState,
    MainModelOutput,
    NodeCapabilityError,
    ResultQuerySpec,
    build_agent_graph,
)
from backend.app.agent.nodes.analysis import analysis_node
from backend.app.agent.nodes.result_qa import result_qa_node


class RecordingCall:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def __call__(self, request: object) -> list[object]:
        self.calls.append(request)
        return []


class RecordingSubmitter:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def __call__(self, request: object) -> object:
        self.calls.append(request)
        return object()


class ScriptedModel:
    def __call__(self, _context: object) -> MainModelOutput:
        return MainModelOutput(
            decision=AgentDecision(action="run_analysis", analysis_type="DEG")
        )


def _state(**updates: object) -> GraphState:
    values: dict[str, object] = {
        "thread_id": "agent-capability",
        "user_id": "user-1",
        "user_message": "Run a DEG analysis",
    }
    values.update(updates)
    return GraphState.model_validate(values)


def test_analysis_node_rejects_query_result_before_any_dependency_call() -> None:
    loader = RecordingCall()
    submitter = RecordingSubmitter()
    state = _state(decision=AgentDecision(
        action="query_result",
        result_query=ResultQuerySpec(artifact="results.csv"),
    ))

    with pytest.raises(NodeCapabilityError, match="Analysis node.*query_result"):
        analysis_node(loader, submitter)(state)

    assert loader.calls == []
    assert submitter.calls == []


def test_result_qa_node_rejects_run_analysis_before_any_dependency_call() -> None:
    reader = RecordingCall()
    querier = RecordingCall()
    state = _state(decision=AgentDecision(action="run_analysis", analysis_type="DEG"))

    with pytest.raises(NodeCapabilityError, match="Result QA node.*run_analysis"):
        result_qa_node(reader, querier)(state)

    assert reader.calls == []
    assert querier.calls == []


def test_main_route_enters_analysis_validation_before_job_submission() -> None:
    submitter = RecordingSubmitter()
    graph = build_agent_graph(
        ScriptedModel(),
        lambda _request: [],
        submitter,
        lambda _request: None,
        lambda _request: None,
    )

    result = graph.invoke(
        _state(),
        {"configurable": {"thread_id": "agent-capability"}},
    )

    assert result["__interrupt__"][0].value["kind"] == "clarification"
    assert submitter.calls == []
