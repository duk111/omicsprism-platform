from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.app.agent.context import MinimalContextBuilder
from backend.app.agent.runtime import (
    ProductionRunCoordinator,
    _job_failure_facts,
    _input_receipt_facts,
    _plan_superseded_facts,
)
from backend.app.agent.schemas import ActiveProfile, AgentState, ModelContext, RunFocus, RunState, RunStatus
from backend.app.agent.tools import AgentInputFile, AgentToolRuntime
from backend.app.models import AnalysisType


def _state() -> RunState:
    return RunState(
        run_id="run-1", user_id="user-1", thread_id="thread-1",
        active_profile=ActiveProfile.ANALYSIS, state=AgentState.AWAIT_FOLLOWUP,
        step_no=0, plan_id=None, plan_hash=None, pending_approval_id=None,
        focus=RunFocus(in_scope_job_ids=[], resolved_entities={}, last_citation=None),
        model_calls=0, tool_calls=0, status=RunStatus.RUNNING, version=0,
    )


def test_preflight_alignment_reports_case_difference_without_raw_content() -> None:
    runtime = AgentToolRuntime(
        user_id="user-1",
        inputs={
            "counts": AgentInputFile("counts.csv", b"gene,S1,S2\ng1,1,2\n"),
            "metadata": AgentInputFile("metadata.csv", b"sample_id,treatment\ns1,control\ns2,salt\n"),
        },
    )
    result = runtime.run_preflight(AnalysisType.DIFFERENTIAL, {
        "compare_field": "treatment", "tested_levels": "salt", "reference_level": "control", "min_replicates": 1,
    })
    alignment = result.rows[0]["alignment"]
    assert alignment["matched"] == 0
    assert alignment["missing_from_metadata"] == ["S1", "S2"]
    assert alignment["extra_in_metadata"] == ["s1", "s2"]
    assert alignment["pattern_hint"] == "大小写差异"
    assert "gene" not in str(alignment)


def test_job_failure_facts_keep_sanitized_error_only() -> None:
    facts = _job_failure_facts([{
        "job_id": "job-1", "analysis_type": "differential", "status": "failed",
        "error": "Traceback (most recent call last): /srv/app/run.py sha256:" + "a" * 64,
    }])
    text = str(facts)
    assert "/srv/app" not in text
    assert "Traceback" not in text
    assert "sha256:" not in text
    assert facts["errors"][0]["advice_category"] == "other"


def test_all_s4_fact_shapes_pass_common_sensitive_scan() -> None:
    builder = MinimalContextBuilder()
    facts = [
        {"situation": "explain_plan", "analysis_type": "differential", "contrasts": [], "effective_params": {}, "expires_in_minutes": 0},
        {"situation": "capability_help", "analysis_capabilities": [], "roles_present": []},
        {"situation": "input_receipt", "roles_present": [], "roles_missing": [], "role_summaries": []},
        _plan_superseded_facts("differential", "unknown", {"compare_field": "treatment"}),
        {"situation": "status_not_running", "has_inputs": False, "has_pending_plan": False},
        _job_failure_facts([]),
    ]
    for item in facts:
        context = builder.build_narration(state=_state(), system_facts=item)
        assert context.available_tools == []
        assert context.system_facts == item


@pytest.mark.parametrize("value", [
    {"situation": "explain_plan", "analysis_type": "differential", "contrasts": [{"compare_field": "postgresql://secret"}], "effective_params": {}, "expires_in_minutes": 0},
    {"situation": "status_not_running", "has_inputs": False, "has_pending_plan": False, "error_text": "C:\\private\\file.csv"},
])
def test_s4_facts_reject_sensitive_strings(value: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ModelContext(
            user_message="facts", active_profile=ActiveProfile.ANALYSIS, state=AgentState.AWAIT_FOLLOWUP,
            in_scope_job_ids=[], available_tools=[], system_facts=value,
        )


def test_narrate_fallback_is_used_when_model_is_unavailable() -> None:
    coordinator = object.__new__(ProductionRunCoordinator)
    fallback = "原有 fallback"
    result = coordinator._narrate(
        state=_state(),
        system_facts={"situation": "status_not_running", "has_inputs": False, "has_pending_plan": False},
        fallback=fallback,
        call_model=lambda _context: (_ for _ in ()).throw(RuntimeError("model unavailable")),
    )
    assert result == fallback
