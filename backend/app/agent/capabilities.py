"""Shared, ownership-bound capability registry for Web Agent and MCP adapters."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .readonly_tools import (
    ArtifactDescriptionResult,
    ContrastEnumeration,
    DescribeArtifactsRequest,
    DescribeMetadataRequest,
    EnumerateContrastsRequest,
    GetJobRequest,
    JobListResult,
    ListJobsRequest,
    MetadataDescription,
    QueryArtifactRequest,
)
from .tools import AgentToolRuntime
from .schemas import ToolResult
from .trace import TOOL_SCHEMA_VERSION


class CapabilityPrincipal(BaseModel):
    """Explicit caller identity; client arguments never carry the owner id."""

    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1, max_length=200)
    transport: Literal["internal", "local", "session", "jwt"] = "internal"
    scopes: list[str] = Field(default_factory=list, max_length=16)


class CapabilitySpec(BaseModel):
    """Stable public description of a registered capability and its schemas."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    description: str = Field(min_length=1, max_length=300)
    read_only: Literal[True] = True
    schema_version: str = Field(default=TOOL_SCHEMA_VERSION, min_length=1, max_length=80)
    request_schema: dict[str, Any]
    response_schema: dict[str, Any]


class CapabilityError(RuntimeError):
    """Stable internal error boundary for capability adapters."""

    code = "capability_error"


class CapabilityNotVisible(CapabilityError):
    """Unknown or unauthorized capabilities share one non-enumerating outcome."""

    code = "not_visible"


class CapabilityInvalidArguments(CapabilityError):
    code = "invalid_arguments"


CapabilityHandler = Callable[[BaseModel], BaseModel]


@dataclass(frozen=True)
class _CapabilityDefinition:
    spec: CapabilitySpec
    request_model: type[BaseModel]
    response_model: type[BaseModel]
    handler: CapabilityHandler


class CapabilityRegistry:
    """Small typed registry shared by internal callers and future MCP adapters."""

    def __init__(self, *, owner_subject: str | None = None) -> None:
        self._owner_subject = owner_subject
        self._definitions: dict[str, _CapabilityDefinition] = {}

    def register(
        self,
        name: str,
        *,
        description: str,
        request_model: type[BaseModel],
        response_model: type[BaseModel],
        handler: CapabilityHandler,
    ) -> None:
        if name in self._definitions:
            raise ValueError(f"capability already registered: {name}")
        spec = CapabilitySpec(
            name=name,
            description=description,
            request_schema=request_model.model_json_schema(),
            response_schema=response_model.model_json_schema(),
        )
        self._definitions[name] = _CapabilityDefinition(
            spec=spec,
            request_model=request_model,
            response_model=response_model,
            handler=handler,
        )

    def specs(self) -> list[CapabilitySpec]:
        return [self._definitions[name].spec for name in sorted(self._definitions)]

    def get(self, name: str) -> CapabilitySpec:
        definition = self._definitions.get(name)
        if definition is None:
            raise CapabilityNotVisible("capability is not available")
        return definition.spec

    def invoke(
        self,
        name: str,
        arguments: Mapping[str, Any] | None,
        *,
        principal: CapabilityPrincipal,
    ) -> BaseModel:
        definition = self._definitions.get(name)
        if definition is None or (
            self._owner_subject is not None and principal.subject != self._owner_subject
        ):
            raise CapabilityNotVisible("capability is not available")
        try:
            request = definition.request_model.model_validate(dict(arguments or {}))
        except Exception as exc:
            raise CapabilityInvalidArguments("capability arguments are invalid") from exc
        result = definition.handler(request)
        try:
            return definition.response_model.model_validate(result)
        except Exception as exc:
            raise CapabilityError("capability response is invalid") from exc


def build_readonly_capability_registry(runtime: AgentToolRuntime) -> CapabilityRegistry:
    """Register only bounded read operations for one ownership-scoped runtime."""

    registry = CapabilityRegistry(owner_subject=runtime.user_id)
    registry.register(
        "describe_metadata",
        description="Describe observed metadata columns and levels.",
        request_model=DescribeMetadataRequest,
        response_model=MetadataDescription,
        handler=lambda request: runtime.describe_metadata(request.fields),
    )
    registry.register(
        "enumerate_contrasts",
        description="Enumerate observed, replicate-checked contrast candidates.",
        request_model=EnumerateContrastsRequest,
        response_model=ContrastEnumeration,
        handler=lambda request: runtime.enumerate_contrasts(
            compare_field=request.compare_field,
            scope=request.scope,
            min_replicates=request.min_replicates,
        ),
    )
    registry.register(
        "list_jobs",
        description="List the caller's bounded analysis Job history.",
        request_model=ListJobsRequest,
        response_model=JobListResult,
        handler=lambda request: runtime.list_jobs(
            analysis_type=request.analysis_type,
            limit=request.limit,
        ),
    )
    registry.register(
        "get_job",
        description="Get one caller-owned Job status summary.",
        request_model=GetJobRequest,
        response_model=ToolResult,
        handler=lambda request: runtime.get_job(request.job_id),
    )
    registry.register(
        "describe_artifacts",
        description="Describe artifacts belonging to one caller-owned Job.",
        request_model=DescribeArtifactsRequest,
        response_model=ArtifactDescriptionResult,
        handler=lambda request: runtime.describe_artifacts(request.job_id),
    )
    registry.register(
        "query_artifact",
        description="Query a bounded caller-owned artifact with citation metadata.",
        request_model=QueryArtifactRequest,
        response_model=ToolResult,
        handler=lambda request: runtime.query_artifact(
            request.job_id,
            request.artifact,
            filters=request.filters,
            field_path=request.field_path,
            sort=request.sort,
            limit=request.limit,
            resolve_entity=request.resolve_entity,
        ),
    )
    return registry
