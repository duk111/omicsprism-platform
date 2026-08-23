from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.app.agent.dataset_profile import MatrixProfile
from backend.app.agent.graph import (
    AgentDecision,
    ClarificationPayload,
    ConfirmationPayload,
    DatasetProfileRef,
    GraphState,
    JobRef,
    StepBudget,
)
from backend.app.agent.param_resolver import DEGParams


def _profile_ref(owner_id: str = "user-1") -> DatasetProfileRef:
    return DatasetProfileRef(
        dataset_id="dataset-1",
        owner_id=owner_id,
        filename="counts.csv",
        checksum="sha256:" + "a" * 64,
        profile=MatrixProfile(
            role="counts",
            shape=(2, 2),
            sample_ids=["s1", "s2"],
            feature_type="gene",
            feature_id_examples=["g1", "g2"],
            numeric_type="integer_counts",
            has_negative=False,
            missing_rate=0,
        ),
    )


def test_agent_decision_uses_v3_action_contract() -> None:
    decision = AgentDecision(action="run_analysis", analysis_type="DEG")

    assert decision.action == "run_analysis"
    assert decision.analysis_type == "DEG"
    assert decision.proposal is None


def test_graph_state_keeps_only_bounded_refs_and_context() -> None:
    state = GraphState(
        thread_id="thread-1",
        user_id="user-1",
        user_message="Run DEG",
        dataset_profiles=[_profile_ref()],
        current_job=JobRef(job_id="job-1", owner_id="user-1"),
        recent_jobs=[JobRef(job_id="job-0", owner_id="user-1")],
    )

    assert state.step_budget.max_steps == 8
    assert state.dataset_profiles[0].profile.shape == (2, 2)
    assert state.current_job is not None
    assert state.current_job.job_id == "job-1"


def test_graph_state_rejects_cross_user_references() -> None:
    with pytest.raises(ValidationError, match="must belong"):
        GraphState(
            thread_id="thread-1",
            user_id="user-1",
            user_message="Show result",
            current_job=JobRef(job_id="job-1", owner_id="user-2"),
        )


def test_graph_state_rejects_raw_dataset_content() -> None:
    with pytest.raises(ValidationError):
        GraphState.model_validate({
            "thread_id": "thread-1",
            "user_id": "user-1",
            "user_message": "Run DEG",
            "raw_dataset": "gene,s1\ng1,10",
        })


def test_step_budget_is_bounded() -> None:
    with pytest.raises(ValidationError):
        StepBudget(max_steps=2, used_steps=3)


def test_pending_interrupt_is_discriminated_and_typed() -> None:
    clarification = ClarificationPayload(
        missing=[{"field": "compare_field", "options": ["condition"], "reason": "Choose a factor"}],
        question="Which factor should be compared?",
    )
    state = GraphState(
        thread_id="thread-1",
        user_id="user-1",
        user_message="Run DEG",
        pending_interrupt=clarification,
    )

    assert state.pending_interrupt is not None
    assert state.pending_interrupt.kind == "clarification"


def test_confirmation_payload_uses_resolved_params_not_a_dict() -> None:
    payload = ConfirmationPayload(
        analysis_type="DEG",
        resolved_params={
            "analysis_type": "DEG",
            "contrast": {
                "compare_field": "condition",
                "tested_level": "salt",
                "reference_level": "control",
            },
        },
        input_fingerprint="sha256:" + "b" * 64,
    )

    assert isinstance(payload.resolved_params, DEGParams)


def test_confirmation_analysis_type_must_match_resolved_params() -> None:
    with pytest.raises(ValidationError, match="must match"):
        ConfirmationPayload(
            analysis_type="DEM",
            resolved_params={
                "analysis_type": "DEG",
                "contrast": {
                    "compare_field": "condition",
                    "tested_level": "salt",
                    "reference_level": "control",
                },
            },
            input_fingerprint="sha256:" + "b" * 64,
        )
