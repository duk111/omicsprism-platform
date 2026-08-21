from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.app.agent.context import MinimalContextBuilder
from backend.app.agent.model import ModelBoundaryError, StructuredModelAdapter
from backend.app.agent.runtime import _narration_numbers_grounded
from backend.app.agent.schemas import (
    ActiveProfile,
    AgentNarrationDecision,
    AgentState,
    ModelContext,
    RunFocus,
    RunState,
    RunStatus,
)


def _state() -> RunState:
    return RunState(
        run_id="run-1",
        user_id="user-1",
        thread_id="thread-1",
        active_profile=ActiveProfile.ANALYSIS,
        state=AgentState.WAIT_EXECUTION_CONFIRMATION,
        step_no=0,
        plan_id="plan-1",
        plan_hash="sha256:test",
        pending_approval_id="approval-1",
        focus=RunFocus(in_scope_job_ids=[], resolved_entities={}, last_citation=None),
        model_calls=0,
        tool_calls=0,
        status=RunStatus.SUSPENDED,
        version=0,
    )


def _pending_facts() -> dict[str, object]:
    return {
        "situation": "pending_approval",
        "analysis_type": "differential",
        "contrast_count": 2,
        "expires_in_minutes": 22,
        "user_options": ["approve", "reject", "modify_params", "explain_plan"],
    }


@pytest.mark.parametrize("value", [
    {**_pending_facts(), "database_url": "postgresql://secret"},
    {**_pending_facts(), "analysis_type": "C:\\private\\counts.csv"},
    {**_pending_facts(), "analysis_type": "uploads/counts.csv"},
    {**_pending_facts(), "analysis_type": "sha256:" + "a" * 64},
    {**_pending_facts(), "analysis_type": "Traceback (most recent call last)"},
    {**_pending_facts(), "analysis_type": "gene,s1\ng1,10"},
])
def test_system_facts_reject_disallowed_or_sensitive_values(value: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        MinimalContextBuilder().build_narration(state=_state(), system_facts=value)


def test_system_facts_enforce_shape_depth_and_size() -> None:
    builder = MinimalContextBuilder()
    with pytest.raises(ValidationError):
        builder.build_narration(
            state=_state(),
            system_facts={**_pending_facts(), "analysis_type": {"a": {"b": {"c": "d"}}}},
        )
    with pytest.raises(ValidationError):
        builder.build_narration(
            state=_state(),
            system_facts={**_pending_facts(), "analysis_type": "x" * 401},
        )


def test_narration_context_has_no_tools_history_inputs_or_jobs() -> None:
    context = MinimalContextBuilder().build_narration(state=_state(), system_facts=_pending_facts())
    assert context.available_tools == []
    assert context.in_scope_job_ids == []
    assert context.conversation_summary is None
    assert context.input_summaries == []
    assert context.system_facts == _pending_facts()


def test_structured_adapter_selects_narration_contract() -> None:
    context = MinimalContextBuilder().build_narration(state=_state(), system_facts=_pending_facts())
    valid = {
        "action": "answer",
        "narration": "计划包含 2 个对比，剩余 22 分钟。",
        "feasibility": None,
        "analysis_recommendations": [],
        "requires_approval": False,
        "requested_params": {},
        "grounded_answer": None,
        "advisory_answer": None,
    }
    assert isinstance(StructuredModelAdapter(lambda _context: valid).decide(context), AgentNarrationDecision)
    with pytest.raises(ModelBoundaryError):
        StructuredModelAdapter(lambda _context: {**valid, "requires_approval": True}).decide(context)


def test_narration_numbers_must_come_from_numeric_facts() -> None:
    facts = _pending_facts()
    assert _narration_numbers_grounded("包含 2 个对比，剩余 22 分钟。", facts)
    assert not _narration_numbers_grounded("包含 3 个对比，剩余 22 分钟。", facts)
    assert not _narration_numbers_grounded("预计 2026 年完成。", facts)
