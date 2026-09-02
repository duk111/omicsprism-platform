from __future__ import annotations

import pytest

from backend.app.agent.capabilities import (
    CapabilityInvalidArguments,
    CapabilityNotVisible,
    CapabilityPrincipal,
    build_readonly_capability_registry,
)
from backend.app.agent.tools import AgentInputFile, AgentToolRuntime


def _runtime() -> AgentToolRuntime:
    metadata = (
        "sample_id,treatment\n"
        "s1,control\n"
        "s2,control\n"
        "s3,salt\n"
        "s4,salt\n"
    ).encode()
    return AgentToolRuntime(
        user_id="user-1",
        inputs={"metadata": AgentInputFile("metadata.csv", metadata)},
    )


def test_registry_exposes_stable_readonly_specs_and_strict_schemas() -> None:
    registry = build_readonly_capability_registry(_runtime())

    assert [spec.name for spec in registry.specs()] == [
        "describe_artifacts",
        "describe_metadata",
        "enumerate_contrasts",
        "get_job",
        "list_jobs",
        "query_artifact",
    ]
    metadata = registry.get("describe_metadata")
    assert metadata.read_only is True
    assert metadata.schema_version == "readonly-tools.v1"
    assert metadata.request_schema["additionalProperties"] is False
    assert metadata.response_schema["additionalProperties"] is False


def test_registry_invokes_typed_capability_without_exposing_principal_argument() -> None:
    registry = build_readonly_capability_registry(_runtime())

    result = registry.invoke(
        "describe_metadata",
        {"fields": ["treatment"]},
        principal=CapabilityPrincipal(subject="user-1", transport="local"),
    )

    assert result.ok is True
    assert [field.field for field in result.fields] == ["treatment"]


def test_registry_rejects_unknown_principal_and_invalid_arguments_uniformly() -> None:
    registry = build_readonly_capability_registry(_runtime())

    with pytest.raises(CapabilityNotVisible) as unauthorized:
        registry.invoke(
            "describe_metadata",
            {},
            principal=CapabilityPrincipal(subject="user-2", transport="jwt"),
        )
    assert str(unauthorized.value) == "capability is not available"

    with pytest.raises(CapabilityInvalidArguments):
        registry.invoke(
            "describe_metadata",
            {"fields": ["treatment"], "unexpected": True},
            principal=CapabilityPrincipal(subject="user-1"),
        )

    with pytest.raises(CapabilityNotVisible) as unknown:
        registry.invoke(
            "not_a_capability",
            {},
            principal=CapabilityPrincipal(subject="user-1"),
        )
    assert str(unknown.value) == str(unauthorized.value)
