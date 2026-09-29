from __future__ import annotations

from backend.app.agent.dataset_profile import MetadataProfile, MatrixProfile
from backend.app.agent.graph import (
    AgentRole,
    AnalysisDecision,
    AnalysisModelOutput,
    CapabilityQueryInput,
    DatasetProfileRef,
    GraphState,
    build_agent_graph,
)
from backend.app.agent.schemas import ToolName


class _CapabilityModel:
    def __call__(self, _context: object, *, role: AgentRole) -> object:
        assert role is AgentRole.ANALYSIS
        return AnalysisModelOutput(decision=AnalysisDecision(
            action="capability_query",
            capability_query=CapabilityQueryInput(analysis_type="DEM"),
        ))


def test_capability_query_uses_canonical_report_and_does_not_call_tools() -> None:
    metadata = MetadataProfile(
        role="metadata",
        columns=["treatment"],
        levels={"treatment": {"control": 2, "salt": 2}},
        sample_ids=["s1", "s2", "s3", "s4"],
        alignment={"metabolome": "exact"},
    )
    metabolome = MatrixProfile(
        role="metabolome",
        shape=(2, 4),
        sample_ids=["s1", "s2", "s3", "s4"],
        feature_type="metabolite",
        feature_id_examples=["M1", "M2"],
        numeric_type="continuous_abundance",
        has_negative=False,
        missing_rate=0,
    )
    state = GraphState(
        thread_id="thread-capability",
        user_id="user-capability",
        user_message="\u8fd9\u4e24\u4e2a\u6570\u636e\u80fd\u505a\u4ec0\u4e48",
        dataset_profiles=[
            DatasetProfileRef(
                dataset_id="metadata-1",
                owner_id="user-capability",
                filename="metadata.csv",
                checksum="sha256:" + "a" * 64,
                profile=metadata,
            ),
            DatasetProfileRef(
                dataset_id="metabolome-1",
                owner_id="user-capability",
                filename="metabolome.csv",
                checksum="sha256:" + "b" * 64,
                profile=metabolome,
            ),
        ],
    )
    tool_calls: list[ToolName] = []

    def execute(request, _state):
        tool_calls.append(request.tool)
        raise AssertionError("capability queries must not call tools")

    graph = build_agent_graph(
        _CapabilityModel(),
        lambda _request: [],
        lambda _request: None,
        lambda _request: None,
        lambda _request: None,
        tool_executor=execute,
    )
    result = GraphState.model_validate(graph.invoke(
        state.model_dump(mode="json"),
        {"configurable": {"thread_id": state.thread_id}},
    ))

    assert result.outcome == "completed"
    assert result.capability_report is not None
    item = result.capability_report.items[0]
    assert item.analysis_type == "DEM"
    assert item.present_roles == ["metabolome", "metadata"]
    assert item.missing_roles == []
    assert item.next_step == "ready_for_parameter_resolution"
    assert "\u53ef\u7ee7\u7eed\u89e3\u6790\u5206\u6790\u53c2\u6570" in (result.response_text or "")
    assert result.current_job is None
    assert tool_calls == []
