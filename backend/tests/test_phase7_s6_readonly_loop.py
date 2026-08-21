from __future__ import annotations

import pytest

from backend.app.agent.policy import ProfilePolicyGuard
from backend.app.agent.schemas import (
    ActiveProfile,
    AgentAction,
    AgentDecision,
    AgentState,
    Feasibility,
    FeasibilityVerdict,
    ModelContext,
    RunFocus,
    RunState,
    RunStatus,
    ToolCallArguments,
    ToolName,
    ToolParamSet,
    ToolResult,
)
from backend.app.agent.context import MinimalContextBuilder
from backend.app.agent.validator import DecisionValidator, InvalidDecision
from backend.tests.test_agent_end_to_end import (
    COUNTS,
    METADATA_TREATMENT,
    _analysis_decision,
    _make_coordinator,
    _RecordingModel,
    _turn,
)


def _state(profile: ActiveProfile) -> RunState:
    return RunState(
        run_id="run-1", user_id="user-1", thread_id="thread-1",
        active_profile=profile, state=(
            AgentState.CHECK_INPUTS if profile is ActiveProfile.ANALYSIS
            else AgentState.ANSWER_WITH_EVIDENCE
        ), step_no=0, plan_id=None, plan_hash=None, pending_approval_id=None,
        focus=RunFocus(in_scope_job_ids=["job-1"], resolved_entities={}, last_citation=None),
        model_calls=0, tool_calls=0, status=RunStatus.RUNNING, version=0,
    )


def _call(tool: ToolName, arguments: ToolCallArguments) -> AgentDecision:
    return AgentDecision(
        action=AgentAction.CALL_TOOL,
        reasoning_summary="read-only lookup",
        feasibility=None,
        analysis_recommendations=[],
        requires_approval=False,
        requested_params={},
        grounded_answer=None,
        advisory_answer=None,
        tool=tool,
        arguments=arguments,
    )


def test_submit_is_not_a_read_only_tool() -> None:
    assert ToolName.SUBMIT_APPROVED_PLAN not in ProfilePolicyGuard.READ_ONLY_TOOLS
    with pytest.raises(InvalidDecision):
        DecisionValidator().validate(
            _state(ActiveProfile.ANALYSIS),
            _call(ToolName.SUBMIT_APPROVED_PLAN, ToolCallArguments()),
        )


def test_interpretation_profile_rejects_preflight() -> None:
    with pytest.raises(InvalidDecision):
        DecisionValidator().validate(
            _state(ActiveProfile.INTERPRETATION),
            _call(
                ToolName.RUN_PREFLIGHT,
                ToolCallArguments(analysis_type="differential", params=ToolParamSet()),
            ),
        )


def test_context_tool_history_drops_oldest_when_byte_budget_is_exceeded() -> None:
    state = _state(ActiveProfile.INTERPRETATION)
    rows = [{"_row_id": 1, "Gene": "G" * 8000}]
    history = [ToolResult(
        tool=ToolName.QUERY_RESULT_EVIDENCE,
        ok=True, rows=rows, truncated=False, row_count=1,
        artifact="result.csv", checksum="sha256:test", filters={}, sort=None, error_code=None,
    ) for _ in range(4)]
    context = MinimalContextBuilder().build(
        state=state,
        active_profile=ActiveProfile.INTERPRETATION,
        user_message="解释结果",
        tool_history=history,
    )
    assert len(context.tool_history) < 4
    assert len(context.model_dump_json().encode("utf-8")) < 32 * 1024


def test_tool_call_arguments_are_structured_and_extra_keys_are_rejected() -> None:
    args = ToolCallArguments(job_ids=["job-1"], limit=4)
    assert args.job_ids == ["job-1"]
    with pytest.raises(ValueError):
        ToolCallArguments.model_validate({"job_ids": ["job-1"], "shell": "cat /etc/passwd"})


def test_analysis_loop_executes_read_only_call_then_keeps_approval_gate() -> None:
    model = _RecordingModel([
        _call(ToolName.GET_ANALYSIS_SPEC, ToolCallArguments(analysis_type="differential")),
        _analysis_decision(),
    ])
    coordinator, _store, _plans, _approvals, jobs, _executor = _make_coordinator(
        model=model,
        inputs={"counts": COUNTS, "metadata": METADATA_TREATMENT},
    )
    result = coordinator.execute_turn(turn=_turn("s6-loop"), user_message="比较 salt 和 control")
    assert result.state.status.value == "suspended"
    assert jobs.saved == []
    assert any(item.tool is ToolName.GET_ANALYSIS_SPEC for item in model.contexts[1].tool_history)


def test_analysis_loop_rejects_submit_decision_without_creating_job() -> None:
    model = _RecordingModel([
        _call(ToolName.SUBMIT_APPROVED_PLAN, ToolCallArguments()),
        _analysis_decision(),
    ])
    coordinator, _store, _plans, _approvals, jobs, executor = _make_coordinator(
        model=model,
        inputs={"counts": COUNTS, "metadata": METADATA_TREATMENT},
    )
    result = coordinator.execute_turn(turn=_turn("s6-submit-rejected"), user_message="比较 salt 和 control")
    assert result.state.status.value == "suspended"
    assert jobs.saved == []
    assert executor.enqueued == []
    assert model.contexts[1].retry_hint
