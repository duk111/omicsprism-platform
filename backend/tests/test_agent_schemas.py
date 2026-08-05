from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from backend.app.agent.schemas import (
    AgentAction,
    AgentDecision,
    AgentState,
    GroundedAnswer,
    RouteDecision,
    RunState,
    ToolResult,
    VerifierVerdict,
)
from backend.app.agent.validator import DecisionValidator, InvalidDecision


VALID_SAMPLES = {
    RouteDecision: {
        "intent": "interpret",
        "target_profile": "interpretation",
        "reason": "已有完成的任务，用户正在追问结果",
    },
    AgentDecision: {
        "action": "propose_plan",
        "reasoning_summary": "现有输入支持生成分析计划",
        "feasibility": {
            "verdict": "answerable",
            "reasons": ["输入类型齐全"],
            "missing_information": [],
        },
        "analysis_recommendations": ["differential", "dem", "correlation"],
        "requires_approval": True,
        "requested_params": {
            "compare_field": "treatment",
            "tested_levels": "salt",
            "reference_level": "control",
        },
    },
    RunState: {
        "run_id": "run-1",
        "user_id": "user-1",
        "thread_id": "thread-1",
        "active_profile": "interpretation",
        "state": "AWAIT_FOLLOWUP",
        "step_no": 8,
        "plan_id": "plan-1",
        "plan_hash": "sha256:plan",
        "pending_approval_id": None,
        "focus": {
            "in_scope_job_ids": ["job-gma-1"],
            "resolved_entities": {"proline": "M0123"},
            "last_citation": {
                "artifact": "T02_High_Confidence_Network.csv",
                "checksum": "sha256:artifact",
                "row_ids": [12, 38],
            },
        },
        "model_calls": 2,
        "tool_calls": 3,
        "status": "running",
        "version": 9,
    },
    ToolResult: {
        "tool": "query_result_evidence",
        "ok": True,
        "rows": [
            {
                "Source": "GeneA",
                "Target": "M0123",
                "EdgeWeight": 0.82,
                "PearsonR": 0.71,
                "RRARank": 3,
                "Sign": "positive",
            }
        ],
        "truncated": False,
        "row_count": 12,
        "artifact": "T02_High_Confidence_Network.csv",
        "checksum": "sha256:artifact",
        "filters": {"Target": "M0123"},
        "sort": "EdgeWeight desc",
        "error_code": None,
    },
    GroundedAnswer: {
        "claims": [
            {
                "text": "与脯氨酸边权最高的基因为 GeneA",
                "citation": {
                    "artifact": "T02_High_Confidence_Network.csv",
                    "checksum": "sha256:artifact",
                    "row_ids": [12],
                },
            }
        ]
    },
    VerifierVerdict: {
        "verdict": "approved",
        "checks": [
            {
                "claim_index": 0,
                "number_matches_evidence": True,
                "citation_valid": True,
                "beyond_evidence": False,
                "issues": [],
            }
        ],
    },
}


@pytest.mark.parametrize(("schema", "payload"), VALID_SAMPLES.items())
def test_valid_spec_sample_is_accepted(schema, payload) -> None:
    assert schema.model_validate(payload)


@pytest.mark.parametrize(("schema", "payload"), VALID_SAMPLES.items())
def test_missing_required_field_is_rejected(schema, payload) -> None:
    invalid = deepcopy(payload)
    invalid.pop(next(iter(invalid)))

    with pytest.raises(ValidationError):
        schema.model_validate(invalid)


@pytest.mark.parametrize(
    ("schema", "payload", "field"),
    [
        (RouteDecision, VALID_SAMPLES[RouteDecision], "intent"),
        (AgentDecision, VALID_SAMPLES[AgentDecision], "action"),
        (RunState, VALID_SAMPLES[RunState], "active_profile"),
        (ToolResult, VALID_SAMPLES[ToolResult], "tool"),
        (VerifierVerdict, VALID_SAMPLES[VerifierVerdict], "verdict"),
    ],
)
def test_invalid_enum_value_is_rejected(schema, payload, field) -> None:
    invalid = deepcopy(payload)
    invalid[field] = "not-a-valid-enum"

    with pytest.raises(ValidationError):
        schema.model_validate(invalid)


def test_unknown_fields_are_rejected() -> None:
    invalid = {**VALID_SAMPLES[RouteDecision], "unexpected": "value"}

    with pytest.raises(ValidationError):
        RouteDecision.model_validate(invalid)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reasoning_summary", "x" * 241),
        ("analysis_recommendations", ["differential", "dem", "correlation", "differential"]),
        ("requested_params", {f"param_{index}": index for index in range(33)}),
        ("requested_params", {"nested": {"not": "a scalar"}}),
    ],
)
def test_agent_decision_rejects_unbounded_model_output(field, value) -> None:
    invalid = deepcopy(VALID_SAMPLES[AgentDecision])
    invalid[field] = value

    with pytest.raises(ValidationError):
        AgentDecision.model_validate(invalid)


def test_agent_decision_json_schema_exposes_guided_output_bounds() -> None:
    schema = AgentDecision.model_json_schema()
    properties = schema["properties"]

    assert properties["reasoning_summary"]["maxLength"] == 240
    assert properties["analysis_recommendations"]["maxItems"] == 3
    assert properties["requested_params"]["maxProperties"] == 32
    advisory_schema = next(
        item for item in properties["advisory_answer"]["anyOf"]
        if item.get("type") == "string"
    )
    assert advisory_schema["maxLength"] == 1200
    feasibility = schema["$defs"]["Feasibility"]["properties"]
    assert feasibility["reasons"]["maxItems"] == 3
    assert feasibility["missing_information"]["maxItems"] == 3


def test_agent_decision_accepts_bounded_advisory_answer() -> None:
    payload = {
        "action": "answer",
        "reasoning_summary": "General biology question",
        "feasibility": None,
        "analysis_recommendations": [],
        "requires_approval": False,
        "requested_params": {},
        "grounded_answer": None,
        "advisory_answer": "ABA is a plant hormone involved in stress responses.",
    }

    decision = AgentDecision.model_validate(payload)

    assert decision.advisory_answer.startswith("ABA")
    with pytest.raises(ValidationError):
        AgentDecision.model_validate({**payload, "advisory_answer": "x" * 1201})


def test_advisory_state_rejects_plans_approval_and_grounded_evidence() -> None:
    state = RunState.model_validate({
        **VALID_SAMPLES[RunState],
        "state": AgentState.ADVISE,
        "plan_id": None,
        "plan_hash": None,
    })
    base = AgentDecision(
        action=AgentAction.ANSWER,
        reasoning_summary="Bounded consultation",
        feasibility=None,
        analysis_recommendations=[],
        requires_approval=False,
        requested_params={},
        grounded_answer=None,
        advisory_answer="Use biological replicates and record the experimental groups.",
    )

    DecisionValidator().validate(state, base)
    invalid_decisions = (
        base.model_copy(update={"requires_approval": True}),
        base.model_copy(update={"requested_params": {"compare_field": "group"}}),
        base.model_copy(update={"analysis_recommendations": ["differential"]}),
        base.model_copy(update={"grounded_answer": GroundedAnswer(claims=[])}),
    )
    for decision in invalid_decisions:
        with pytest.raises(InvalidDecision):
            DecisionValidator().validate(state, decision)
