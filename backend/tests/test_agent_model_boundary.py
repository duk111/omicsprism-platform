from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from backend.app.agent.model import (
    ModelBoundaryError,
    ModelUnavailableError,
    StructuredModelAdapter,
    UnavailableModelAdapter,
    _decision_adapter,
)
from backend.app.agent.schemas import AgentAction
from backend.app.agent.schemas import ActiveProfile, AgentState, ModelContext, ToolName
from backend.app.bootstrap import create_context
from backend.app.models import AnalysisType, JobRecord, JobStatus
from backend.app.settings import AppSettings


def _valid_decision() -> dict[str, object]:
    return {
        "action": "request_more_data",
        "reasoning_summary": "需要补充样本分组信息",
        "feasibility": None,
        "analysis_recommendations": ["differential"],
        "requires_approval": False,
        "requested_params": {},
    }


def _context() -> ModelContext:
    return ModelContext(
        user_message="请解释结果",
        active_profile=ActiveProfile.INTERPRETATION,
        state=AgentState.ANSWER_WITH_EVIDENCE,
        in_scope_job_ids=["job-1"],
        available_tools=[ToolName.GET_JOBS_STATUS, ToolName.QUERY_RESULT_EVIDENCE],
    )


def test_model_receives_only_serializable_context_and_returns_validated_decision() -> None:
    received: dict[str, object] = {}

    def complete(context):
        received.update(context)
        return _valid_decision()

    decision = StructuredModelAdapter(complete).decide(_context())

    assert received == {
        "user_message": "请解释结果",
        "active_profile": "interpretation",
        "state": "ANSWER_WITH_EVIDENCE",
        "in_scope_job_ids": ["job-1"],
        "available_result_artifacts": [],
        "conversation_summary": None,
        "available_input_roles": [],
        "input_summaries": [],
        "analysis_capabilities": [],
        "available_tools": ["get_jobs_status", "query_result_evidence"],
        "evidence": None,
        "confirmed_params": {},
        "retry_hint": None,
    }
    assert decision.action is AgentAction.REQUEST_MORE_DATA


@pytest.mark.parametrize(
    "extra",
    [
        {"database_url": "postgresql://forbidden"},
        {"raw_file_path": "/data/input.csv"},
        {"repository": object()},
    ],
)
def test_model_context_rejects_handles_credentials_and_raw_paths(extra) -> None:
    payload = _context().model_dump()
    payload.update(extra)

    with pytest.raises(ValidationError):
        ModelContext.model_validate(payload)


def test_model_response_must_match_agent_decision_contract() -> None:
    adapter = StructuredModelAdapter(lambda _context: {"action": "not-an-action"})

    with pytest.raises(ModelBoundaryError):
        adapter.decide(_context())


def test_invalid_model_response_is_repaired_at_most_once() -> None:
    repair_calls = 0

    def repair(_context, invalid_response):
        nonlocal repair_calls
        repair_calls += 1
        assert invalid_response == {"action": "not-an-action"}
        return _valid_decision()

    adapter = StructuredModelAdapter(
        lambda _context: {"action": "not-an-action"},
        repair=repair,
    )

    assert adapter.decide(_context()).action is AgentAction.REQUEST_MORE_DATA
    assert repair_calls == 1


def test_interpretation_adapter_accepts_only_an_evidence_query_shape() -> None:
    payload = {
        "action": "answer",
        "reasoning_summary": "先查询结果证据",
        "feasibility": None,
        "analysis_recommendations": [],
        "requires_approval": False,
        "requested_params": {"job_id": "job-1", "artifact": "deg.csv"},
        "grounded_answer": None,
        "advisory_answer": None,
    }

    decision = _decision_adapter(_context().model_dump()).validate_python(payload)

    assert decision.action == AgentAction.ANSWER.value
    assert decision.requested_params["job_id"] == "job-1"


def test_invalid_repair_is_not_retried() -> None:
    repair_calls = 0

    def repair(_context, _invalid_response):
        nonlocal repair_calls
        repair_calls += 1
        return {"action": "still-invalid"}

    adapter = StructuredModelAdapter(
        lambda _context: {"action": "invalid"},
        repair=repair,
    )

    with pytest.raises(ModelBoundaryError):
        adapter.decide(_context())
    assert repair_calls == 1


def test_model_off_is_explicit_and_does_not_create_a_fake_decision() -> None:
    with pytest.raises(ModelUnavailableError, match="not configured"):
        UnavailableModelAdapter().decide(_context())


def test_model_off_does_not_block_manual_job_storage(tmp_path) -> None:
    settings = AppSettings(
        runs_dir=tmp_path / "runs",
        file_storage_root=tmp_path / "storage",
    )
    context = create_context(
        settings,
        ensure_figure_specs=lambda _job_id: {},
        remaining_seconds=lambda _job, _progress: None,
        include_executor=False,
    )
    now = datetime.now(timezone.utc)
    job = JobRecord(
        id="manual-job",
        project_name="manual workflow",
        analysis_type=AnalysisType.DIFFERENTIAL,
        status=JobStatus.QUEUED,
        created_at=now,
        updated_at=now,
        owner_id="manual-user",
    )

    context.job_store.save(job)

    assert context.job_store.get_for_user("manual-job", "manual-user").id == "manual-job"
