from __future__ import annotations

import pytest

from backend.app.agent.schemas import ToolName
from backend.app.agent.tools import ToolRegistry


TOOL_CALLS = {
    ToolName.INSPECT_UPLOADED_INPUTS: {},
    ToolName.GET_ANALYSIS_SPEC: {"analysis_type": "differential"},
    ToolName.RUN_PREFLIGHT: {"analysis_type": "differential", "params": {}},
    ToolName.SUBMIT_APPROVED_PLAN: {"plan_id": "plan-1", "idempotency_key": "key-1"},
    ToolName.GET_JOBS_STATUS: {"job_ids": ["job-1"]},
    ToolName.QUERY_RESULT_EVIDENCE: {
        "job_id": "job-1",
        "artifact": "T02_High_Confidence_Network.csv",
    },
}


def test_registry_contains_exactly_six_tools() -> None:
    registry = ToolRegistry()

    assert set(registry.names()) == set(ToolName)
    assert len(registry.names()) == 6


@pytest.mark.parametrize(("tool_name", "kwargs"), TOOL_CALLS.items())
def test_every_registered_tool_is_explicitly_unimplemented(tool_name, kwargs) -> None:
    registry = ToolRegistry()

    with pytest.raises(NotImplementedError):
        registry.call(tool_name, **kwargs)
