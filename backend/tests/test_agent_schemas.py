from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from backend.app.agent.schemas import (
    AgentDecision,
    GroundedAnswer,
    RouteDecision,
    RunState,
    ToolResult,
    VerifierVerdict,
)


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
