from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..models import AnalysisType, FileArtifactKind, JobStatus
from .param_resolver import ScopeSpec


ScalarValue = str | int | float | bool | None
MetadataSemanticType = Literal["identifier", "categorical", "numeric", "text", "unknown"]
ArtifactFormat = Literal["csv", "json", "text", "binary", "unknown"]
ToolParamValue = str | int | float | bool | None


class ReadOnlyToolModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DescribeMetadataRequest(ReadOnlyToolModel):
    fields: list[str] | None = Field(default=None, max_length=20)


class EnumerateContrastsRequest(ReadOnlyToolModel):
    compare_field: str | None = Field(default=None, max_length=200)
    scope: ScopeSpec
    min_replicates: int = Field(default=2, ge=1)


class ListJobsRequest(ReadOnlyToolModel):
    analysis_type: AnalysisType | None = None
    limit: int = Field(default=20, ge=1, le=20)


class DescribeArtifactsRequest(ReadOnlyToolModel):
    job_id: str = Field(min_length=1, max_length=200)


class GetJobRequest(ReadOnlyToolModel):
    job_id: str = Field(min_length=1, max_length=200)


class QueryArtifactRequest(ReadOnlyToolModel):
    job_id: str = Field(min_length=1, max_length=200)
    artifact: str = Field(min_length=1, max_length=500)
    filters: dict[str, ToolParamValue] = Field(default_factory=dict, max_length=8)
    field_path: str | None = Field(default=None, max_length=200)
    sort: str | None = Field(default=None, max_length=100)
    limit: int | None = Field(default=None, ge=1, le=50)
    resolve_entity: str | None = Field(default=None, max_length=200)


class MetadataFieldDescription(ReadOnlyToolModel):
    field: str = Field(min_length=1, max_length=200)
    semantic_type: MetadataSemanticType
    levels: dict[str, int] = Field(default_factory=dict, max_length=50)
    sample_count: int = Field(default=0, ge=0)
    missing_count: int = Field(default=0, ge=0)


class MetadataDescription(ReadOnlyToolModel):
    """Deterministic metadata facts returned to the model boundary."""

    ok: bool = True
    fields: list[MetadataFieldDescription] = Field(default_factory=list, max_length=20)
    alignment: dict[str, str] = Field(default_factory=dict, max_length=20)
    sample_count: int = Field(default=0, ge=0)
    truncated: bool = False
    error_code: str | None = Field(default=None, max_length=100)


class ContrastCandidate(ReadOnlyToolModel):
    compare_field: str = Field(min_length=1, max_length=200)
    tested_level: str = Field(min_length=1, max_length=200)
    reference_level: str = Field(min_length=1, max_length=200)
    scope: ScopeSpec
    stratum: dict[str, str] = Field(default_factory=dict, max_length=16)
    tested_count: int = Field(default=0, ge=0)
    reference_count: int = Field(default=0, ge=0)
    executable: bool
    reason: str | None = Field(default=None, max_length=300)


class ContrastEnumeration(ReadOnlyToolModel):
    """All observed contrast directions and their per-stratum facts."""

    ok: bool = True
    compare_field: str | None = Field(default=None, max_length=200)
    scope: ScopeSpec
    min_replicates: int = Field(ge=1)
    candidates: list[ContrastCandidate] = Field(default_factory=list, max_length=200)
    truncated: bool = False
    error_code: str | None = Field(default=None, max_length=100)


class JobListItem(ReadOnlyToolModel):
    job_id: str = Field(min_length=1, max_length=200)
    analysis_type: AnalysisType
    status: JobStatus
    created_at: datetime
    updated_at: datetime
    progress: int = Field(ge=0, le=100)
    params: dict[str, ScalarValue] = Field(default_factory=dict, max_length=32)
    artifacts: list[str] = Field(default_factory=list, max_length=20)


class JobListResult(ReadOnlyToolModel):
    ok: bool = True
    jobs: list[JobListItem] = Field(default_factory=list, max_length=20)
    limit: int = Field(ge=1, le=20)
    truncated: bool = False
    error_code: str | None = Field(default=None, max_length=100)


class ArtifactSchema(ReadOnlyToolModel):
    format: ArtifactFormat
    columns: list[str] = Field(default_factory=list, max_length=100)
    column_types: dict[str, str] = Field(default_factory=dict, max_length=100)


class ArtifactDescription(ReadOnlyToolModel):
    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        serialize_by_alias=True,
    )

    path: str = Field(min_length=1, max_length=500)
    filename: str = Field(min_length=1, max_length=500)
    kind: FileArtifactKind
    checksum: str | None = Field(default=None, max_length=200)
    size_bytes: int = Field(default=0, ge=0)
    content_type: str | None = Field(default=None, max_length=200)
    artifact_schema: ArtifactSchema = Field(alias="schema")

    @property
    def schema(self) -> ArtifactSchema:
        return self.artifact_schema


class ArtifactDescriptionResult(ReadOnlyToolModel):
    ok: bool = True
    job_id: str = Field(min_length=1, max_length=200)
    artifacts: list[ArtifactDescription] = Field(default_factory=list, max_length=50)
    truncated: bool = False
    error_code: str | None = Field(default=None, max_length=100)
