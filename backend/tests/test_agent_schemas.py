from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from backend.app.agent.graph import AgentDecision
from backend.app.agent.schemas import (
    GroundedAnswer,
    RunState,
    ToolResult,
    VerifierVerdict,
)


VALID_SAMPLES = {
    RunState: {
        "run_id": "run-1",
        "user_id": "user-1",
        "thread_id": "thread-1",
        "focus": {
            "in_scope_job_ids": ["job-gma-1"],
            "resolved_entities": {"proline": "M0123"},
            "last_citation": {
                "artifact": "T02_High_Confidence_Network.csv",
                "checksum": "sha256:artifact",
                "row_ids": [12, 38],
            },
        },
        "version": 1,
    },
    ToolResult: {
        "tool": "query_result_evidence",
        "ok": True,
        "rows": [{"Target": "M0123", "PearsonR": 0.71}],
        "truncated": False,
        "row_count": 1,
        "artifact": "T02_High_Confidence_Network.csv",
        "checksum": "sha256:artifact",
        "filters": {"Target": "M0123"},
        "sort": "PearsonR desc",
        "error_code": None,
    },
    GroundedAnswer: {
        "claims": [{
            "text": "GeneA has the strongest association.",
            "citation": {
                "artifact": "T02_High_Confidence_Network.csv",
                "checksum": "sha256:artifact",
                "row_ids": [12],
            },
        }],
    },
    VerifierVerdict: {
        "verdict": "approved",
        "checks": [{
            "claim_index": 0,
            "number_matches_evidence": True,
            "citation_valid": True,
            "beyond_evidence": False,
            "issues": [],
        }],
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
    invalid = {**VALID_SAMPLES[RunState], "unexpected": "value"}

    with pytest.raises(ValidationError):
        RunState.model_validate(invalid)


def test_graph_agent_decision_is_the_single_dispatch_contract() -> None:
    decision = AgentDecision(action="get_job", job_id="job-1")

    assert decision.action == "get_job"
    assert not hasattr(decision, "requires_approval")
