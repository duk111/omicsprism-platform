from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from pathlib import Path
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..models import AnalysisType, FileArtifactInfo, JobRecord
from .param_resolver import ScopeSpec
from .readonly_tools import (
    ArtifactDescription,
    ArtifactDescriptionResult,
    ArtifactSchema,
    ContrastCandidate,
    ContrastEnumeration,
    DescribeMetadataRequest,
    DescribeArtifactsRequest,
    EnumerateContrastsRequest,
    ListJobsRequest,
    JobListItem,
    JobListResult,
    MetadataDescription,
    MetadataFieldDescription,
    QueryArtifactRequest,
)
from .dataset_profile import DatasetProfile, MatrixProfile, MetadataProfile, build_dataset_profiles
from .schemas import ToolName, ToolResult


MAX_TOOL_ROWS = 50
MAX_TOOL_BYTES = 32 * 1024

_FIGURE_JSON_BY_ANALYSIS = {
    AnalysisType.DEG: frozenset({"volcano.json"}),
    AnalysisType.DEM: frozenset({"volcano.json"}),
    AnalysisType.GMA: frozenset({
        "pca.json",
        "dendrogram.json",
        "upset.json",
        "bubble-heatmap.json",
        "scatter-panels.json",
        "violin-box.json",
        "corr-heatmap.json",
        "ridge.json",
        "line-panels.json",
        "circos.json",
    }),
}
_JSON_PATH_PART = re.compile(r"^[A-Za-z0-9_-]+$")
_JSON_ENTITY_FIELDS = (
    "id", "sample_id", "entity_id", "feature", "gene", "metabolite",
    "node_id", "label", "name", "text",
)


class ToolConfigurationError(RuntimeError):
    """工具缺少受控运行时依赖，不能执行。"""


@dataclass(frozen=True)
class AgentInputFile:
    filename: str
    content: bytes


class AgentJobStore(Protocol):
    def get_for_user(self, job_id: str, user_id: str) -> JobRecord:
        ...

    def list_for_user(self, user_id: str, include_deleted: bool = False) -> list[JobRecord]:
        ...


class AgentFileService(Protocol):
    def recent_log(self, job_id: str):
        ...

    def read_artifact_text(self, job_id: str, relative_path: str, *, max_chars: int | None = None) -> str:
        ...


@dataclass
class AgentToolRuntime:
    """Ownership-bound implementations of the graph's direct data capabilities."""

    user_id: str
    inputs: Mapping[str, AgentInputFile] = field(default_factory=dict)
    job_store: AgentJobStore | None = None
    files: AgentFileService | None = None

    def __post_init__(self) -> None:
        if not self.user_id:
            raise ToolConfigurationError("session user_id is required")

    def inspect_dataset(self) -> list[DatasetProfile]:
        """Return bounded, typed facts without exposing raw input contents."""
        return build_dataset_profiles({
            field: (item.filename, item.content)
            for field, item in sorted(self.inputs.items())
        })

    def describe_metadata(self, fields: Sequence[str] | None = None) -> MetadataDescription:
        """Describe observed metadata columns, levels, and matrix alignment."""
        request = DescribeMetadataRequest(fields=list(fields) if fields is not None and not isinstance(fields, str) else ([fields] if isinstance(fields, str) else None))
        profiles = self.inspect_dataset()
        metadata = next(
            (item for item in profiles if getattr(item, "role", None) == "metadata"),
            None,
        )
        if metadata is None:
            metadata = next(
                (item for item in profiles if getattr(item, "role", None) == "group"),
                None,
            )
        if metadata is None:
            return MetadataDescription(
                ok=False,
                fields=[],
                alignment={},
                sample_count=0,
                error_code="metadata_not_found",
            )

        requested = _requested_fields(request.fields)
        available = set(metadata.columns)
        unknown = [name for name in requested if name not in available]
        if unknown:
            return MetadataDescription(
                ok=False,
                fields=[],
                alignment=dict(metadata.alignment),
                sample_count=len(metadata.sample_ids),
                error_code="field_not_found",
            )

        headers, records = _metadata_records(self, metadata)
        selected = requested or list(metadata.columns)
        descriptions: list[MetadataFieldDescription] = []
        field_truncated = len(selected) > 20
        for name in selected[:20]:
            values = [record.get(name, "").strip() for record in records]
            observed = [value for value in values if value]
            counts: dict[str, int] = {}
            for value in observed:
                counts[value] = counts.get(value, 0) + 1
            if not counts:
                counts = dict(metadata.levels.get(name, {}))
            level_items = sorted(counts.items(), key=lambda item: item[0].casefold())
            field_truncated = field_truncated or len(level_items) > 50
            descriptions.append(MetadataFieldDescription(
                field=name,
                semantic_type=_metadata_semantic_type(name, observed, counts),
                levels=dict(level_items[:50]),
                sample_count=len(values) if values else len(metadata.sample_ids),
                missing_count=sum(1 for value in values if not value),
            ))
        return MetadataDescription(
            ok=True,
            fields=descriptions,
            alignment=dict(metadata.alignment),
            sample_count=len(records) if records else len(metadata.sample_ids),
            truncated=field_truncated,
        )

    def enumerate_contrasts(
        self,
        *,
        compare_field: str | None = None,
        scope: ScopeSpec,
        min_replicates: int = 2,
    ) -> ContrastEnumeration:
        """Enumerate observed contrast directions and per-stratum replicate facts."""
        request = EnumerateContrastsRequest(
            compare_field=compare_field,
            scope=scope,
            min_replicates=min_replicates,
        )
        compare_field = request.compare_field
        scope = request.scope
        min_replicates = request.min_replicates
        profiles = self.inspect_dataset()
        metadata = next(
            (item for item in profiles if getattr(item, "role", None) == "metadata"),
            None,
        )
        if metadata is None:
            metadata = next(
                (item for item in profiles if getattr(item, "role", None) == "group"),
                None,
            )
        if metadata is None:
            return ContrastEnumeration(
                ok=False,
                compare_field=compare_field,
                scope=scope,
                min_replicates=min_replicates,
                error_code="metadata_not_found",
            )

        headers, records = _metadata_records(self, metadata)
        metadata_fields = [field for field in headers if field and field != "sample_id"]
        if not metadata_fields:
            metadata_fields = [field for field in metadata.columns if field != "sample_id"]
        if compare_field and compare_field not in metadata_fields:
            return ContrastEnumeration(
                ok=False,
                compare_field=compare_field,
                scope=scope,
                min_replicates=min_replicates,
                error_code="compare_field_not_found",
            )
        if compare_field and (compare_field in scope.blocking_fields or compare_field in scope.fixed_filters):
            return ContrastEnumeration(
                ok=False,
                compare_field=compare_field,
                scope=scope,
                min_replicates=min_replicates,
                error_code="compare_field_in_scope",
            )
        scope_error = _validate_scope(scope, metadata_fields, records)
        if scope_error:
            return ContrastEnumeration(
                ok=False,
                compare_field=compare_field,
                scope=scope,
                min_replicates=min_replicates,
                error_code=scope_error,
            )
        if scope.mode == "unknown":
            return ContrastEnumeration(
                ok=False,
                compare_field=compare_field,
                scope=scope,
                min_replicates=min_replicates,
                error_code="scope_unknown",
            )

        aligned = _aligned_metadata_records(self, metadata, records)
        scoped = _scope_records(aligned, scope)
        fields = [compare_field] if compare_field else metadata_fields
        candidates: list[ContrastCandidate] = []
        for field_name in fields:
            if not field_name or field_name in scope.blocking_fields or field_name in scope.fixed_filters:
                continue
            for stratum, rows in scoped:
                levels = sorted(
                    {row.get(field_name, "").strip() for row in rows if row.get(field_name, "").strip()},
                    key=str.casefold,
                )
                for left_index, left in enumerate(levels):
                    for right in levels[left_index + 1:]:
                        for tested, reference in ((left, right), (right, left)):
                            tested_count = sum(row.get(field_name, "").strip() == tested for row in rows)
                            reference_count = sum(row.get(field_name, "").strip() == reference for row in rows)
                            executable = tested_count >= min_replicates and reference_count >= min_replicates
                            reason = None if executable else (
                                f"requires {min_replicates} replicates per level; "
                                f"observed {tested_count} tested and {reference_count} reference"
                            )
                            candidates.append(ContrastCandidate(
                                compare_field=field_name,
                                tested_level=tested,
                                reference_level=reference,
                                scope=scope,
                                stratum=dict(stratum),
                                tested_count=tested_count,
                                reference_count=reference_count,
                                executable=executable,
                                reason=reason,
                            ))
        truncated = len(candidates) > 200
        return ContrastEnumeration(
            ok=True,
            compare_field=compare_field,
            scope=scope,
            min_replicates=min_replicates,
            candidates=candidates[:200],
            truncated=truncated,
        )

    def list_jobs(
        self,
        *,
        analysis_type: AnalysisType | str | None = None,
        limit: int = 20,
    ) -> JobListResult:
        """Return the current user's bounded Job history and parameter summaries."""
        if self.job_store is None or not hasattr(self.job_store, "list_for_user"):
            raise ToolConfigurationError("job services are not configured")
        normalized_type: AnalysisType | None = None
        if analysis_type is not None:
            try:
                normalized_type = analysis_type if isinstance(analysis_type, AnalysisType) else AnalysisType(str(analysis_type).lower())
            except ValueError:
                return JobListResult(ok=False, jobs=[], limit=limit, error_code="invalid_analysis_type")
        request = ListJobsRequest(analysis_type=normalized_type, limit=limit)
        limit = request.limit
        jobs = self.job_store.list_for_user(self.user_id, include_deleted=False)
        if normalized_type is not None:
            jobs = [job for job in jobs if job.analysis_type == normalized_type]
        jobs = [job for job in jobs if job.owner_id == self.user_id]
        jobs.sort(key=lambda item: item.created_at, reverse=True)
        truncated = len(jobs) > limit
        items = [
            JobListItem(
                job_id=job.id,
                analysis_type=job.analysis_type,
                status=job.status,
                created_at=job.created_at,
                updated_at=job.updated_at,
                progress=job.progress,
                params=_scalar_params(job.params),
                artifacts=_job_artifact_names(job)[:20],
            )
            for job in jobs[:limit]
        ]
        return JobListResult(ok=True, jobs=items, limit=limit, truncated=truncated)

    def describe_artifacts(self, job_id: str) -> ArtifactDescriptionResult:
        """Describe owned Job artifacts and bounded CSV/JSON schemas."""
        job_id = DescribeArtifactsRequest(job_id=job_id).job_id
        if self.job_store is None:
            raise ToolConfigurationError("job services are not configured")
        try:
            job = self.job_store.get_for_user(job_id, self.user_id)
        except LookupError:
            return ArtifactDescriptionResult(ok=False, job_id=job_id, artifacts=[], error_code="not_found")
        except Exception as exc:
            if getattr(exc, "status_code", None) == 404:
                return ArtifactDescriptionResult(ok=False, job_id=job_id, artifacts=[], error_code="not_found")
            raise
        artifacts = _unique_artifacts(job)
        descriptions = [
            ArtifactDescription(
                path=item.path,
                filename=item.filename,
                kind=item.kind,
                checksum=item.checksum,
                size_bytes=item.size_bytes,
                content_type=item.content_type,
                schema=self._artifact_schema(job.id, item),
            )
            for item in artifacts[:50]
        ]
        return ArtifactDescriptionResult(
            ok=True,
            job_id=job.id,
            artifacts=descriptions,
            truncated=len(artifacts) > 50,
        )

    def _artifact_schema(self, job_id: str, artifact: FileArtifactInfo) -> ArtifactSchema:
        suffix = Path(artifact.path).suffix.lower()
        if suffix == ".csv":
            format_name = "csv"
        elif suffix == ".json":
            format_name = "json"
        elif suffix in {".txt", ".log", ".tsv", ".html"}:
            format_name = "text"
        else:
            format_name = "binary"
        if self.files is None or format_name not in {"csv", "json"} or (artifact.size_bytes and artifact.size_bytes > 256_000):
            return ArtifactSchema(format=format_name)
        try:
            text = self.files.read_artifact_text(job_id, artifact.path)
        except Exception:
            return ArtifactSchema(format=format_name)
        if len(text) > 32_768:
            text = text[:32_768]
        return _infer_artifact_schema(format_name, text)

    def get_job(self, job_id: str) -> ToolResult:
        if self.job_store is None:
            raise ToolConfigurationError("job services are not configured")
        try:
            job = self.job_store.get_for_user(job_id, self.user_id)
        except LookupError:
            return _tool_result(ToolName.GET_JOBS_STATUS, rows=[], ok=False, error_code="not_found")
        log_name = log_excerpt = None
        if self.files is not None and hasattr(self.files, "recent_log"):
            log_name, log_excerpt = self.files.recent_log(job.id)
        return _tool_result(ToolName.GET_JOBS_STATUS, rows=[{
            "job_id": job.id,
            "status": job.status.value,
            "progress": job.progress,
            "progress_step": job.progress_step,
            "error": job.error,
            "log_name": log_name,
            "log_excerpt": log_excerpt,
            "artifacts": sorted({
                str(name)
                for item in [*job.artifacts, *job.result_files]
                for name in (getattr(item, "path", None), getattr(item, "filename", None))
                if name and _allowed_result_artifact(str(name), job.analysis_type)
            })[:20],
        }])

    def query_result(
        self,
        job_id: str,
        artifact: str,
        filters: Mapping[str, Any] | None = None,
        field_path: str | None = None,
        sort: str | None = None,
        limit: int | None = None,
        resolve_entity: str | None = None,
    ) -> ToolResult:
        return self._query_artifact(
            job_id,
            artifact,
            filters=filters,
            field_path=field_path,
            sort=sort,
            limit=limit,
            resolve_entity=resolve_entity,
            result_tool=ToolName.QUERY_RESULT_EVIDENCE,
        )

    def query_artifact(
        self,
        job_id: str,
        artifact: str,
        filters: Mapping[str, Any] | None = None,
        field_path: str | None = None,
        sort: str | None = None,
        limit: int | None = None,
        resolve_entity: str | None = None,
    ) -> ToolResult:
        """Query an owned artifact with bounded rows and citation metadata."""
        request = QueryArtifactRequest(
            job_id=job_id,
            artifact=artifact,
            filters=dict(filters or {}),
            field_path=field_path,
            sort=sort,
            limit=limit,
            resolve_entity=resolve_entity,
        )
        return self._query_artifact(
            request.job_id,
            request.artifact,
            filters=request.filters,
            field_path=request.field_path,
            sort=request.sort,
            limit=request.limit,
            resolve_entity=request.resolve_entity,
            result_tool=ToolName.QUERY_ARTIFACT,
        )

    def _query_artifact(
        self,
        job_id: str,
        artifact: str,
        *,
        filters: Mapping[str, Any] | None,
        field_path: str | None,
        sort: str | None,
        limit: int | None,
        resolve_entity: str | None,
        result_tool: ToolName,
    ) -> ToolResult:
        if self.job_store is None or self.files is None:
            raise ToolConfigurationError("result services are not configured")
        if not _allowed_result_artifact(artifact, job_type=None):
            return _tool_result(result_tool, rows=[], ok=False, error_code="artifact_not_allowed")
        job = self.job_store.get_for_user(job_id, self.user_id)
        if not _allowed_result_artifact(artifact, job_type=job.analysis_type):
            return _tool_result(result_tool, rows=[], ok=False, error_code="artifact_not_allowed")
        artifact_info = next(
            (item for item in [*job.artifacts, *job.result_files]
             if item.path == artifact or item.filename == artifact or getattr(item, "name", None) == artifact),
            None,
        )
        if artifact_info is None:
            return _tool_result(result_tool, rows=[], ok=False, error_code="not_found")
        text = self.files.read_artifact_text(job.id, artifact_info.path)
        if Path(artifact).suffix.lower() == ".json":
            return _query_json_evidence(
                text=text,
                artifact=artifact_info.path,
                checksum=artifact_info.checksum,
                field_path=field_path,
                filters=filters,
                sort=sort,
                limit=limit,
                resolve_entity=resolve_entity,
                result_tool=result_tool,
            )
        reader = csv.DictReader(io.StringIO(text))
        rows = [
            {"_row_id": row_id, **{str(key): value for key, value in row.items() if key is not None}}
            for row_id, row in enumerate(reader, start=1)
        ]
        effective_filters = dict(filters or {})
        if resolve_entity:
            effective_filters.setdefault("Gene", resolve_entity)
        rows = [row for row in rows if all(str(row.get(key, "")) == str(value) for key, value in effective_filters.items())]
        if sort:
            sort_field, _, direction = sort.partition(" ")
            reverse = direction.strip().lower() == "desc"
            if not all(sort_field in row for row in rows):
                return _tool_result(result_tool, rows=[], ok=False, error_code="invalid_sort")
            rows.sort(key=lambda row: _sort_value(row.get(sort_field)), reverse=reverse)
        max_rows = min(MAX_TOOL_ROWS, max(1, int(limit or MAX_TOOL_ROWS)))
        selected = rows[:max_rows]
        result = _tool_result(
            result_tool,
            rows=selected,
            artifact=artifact_info.path,
            checksum=artifact_info.checksum or "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest(),
            filters=effective_filters,
            sort=sort,
        )
        result.row_count = len(rows)
        result.truncated = result.truncated or len(selected) < len(rows)
        return result


def _requested_fields(fields: Sequence[str] | None) -> list[str]:
    if fields is None:
        return []
    if isinstance(fields, str):
        fields = [fields]
    result: list[str] = []
    for field_name in fields:
        value = str(field_name).strip()
        if value and value not in result:
            result.append(value)
    return result


def _metadata_records(
    runtime: AgentToolRuntime,
    metadata: MetadataProfile,
) -> tuple[list[str], list[dict[str, str]]]:
    source = runtime.inputs.get(metadata.role)
    if source is not None:
        parsed = list(csv.reader(io.StringIO(source.content.decode("utf-8-sig", errors="replace"))))
        headers = [str(value).strip() for value in (parsed[0] if parsed else [])]
        reader = csv.DictReader(io.StringIO(source.content.decode("utf-8-sig", errors="replace")))
        records = [
            {str(key).strip(): (value or "").strip() for key, value in row.items() if key is not None}
            for row in reader
        ]
        return headers, records
    headers = list(metadata.columns)
    records = [
        {
            header: (row[index].strip() if index < len(row) else "")
            for index, header in enumerate(headers)
        }
        for row in (metadata.rows or [])
    ]
    return headers, records


def _metadata_semantic_type(
    field_name: str,
    values: Sequence[str],
    levels: Mapping[str, int],
) -> str:
    if field_name.casefold() in {"sample_id", "sample", "sampleid"}:
        return "identifier"
    if values:
        numeric = True
        for value in values:
            try:
                float(value)
            except (TypeError, ValueError):
                numeric = False
                break
        if numeric:
            return "numeric"
    if levels:
        return "categorical"
    return "unknown"


def _validate_scope(
    scope: ScopeSpec,
    metadata_fields: Sequence[str],
    records: Sequence[Mapping[str, str]],
) -> str | None:
    if scope.mode == "unknown":
        return "scope_unknown"
    available = set(metadata_fields)
    requested = (
        list(scope.fixed_filters)
        if scope.mode == "fixed"
        else list(scope.blocking_fields)
    )
    if any(field not in available for field in requested):
        return "scope_field_not_found"
    if scope.mode == "fixed":
        if any(
            value not in {row.get(field, "").strip() for row in records}
            for field, value in scope.fixed_filters.items()
        ):
            return "scope_value_not_found"
    return None


def _aligned_metadata_records(
    runtime: AgentToolRuntime,
    metadata: MetadataProfile,
    records: Sequence[Mapping[str, str]],
) -> list[dict[str, str]]:
    matrix_ids: set[str] = set()
    for profile in runtime.inspect_dataset():
        if isinstance(profile, MatrixProfile):
            matrix_ids.update(profile.sample_ids)
    if not matrix_ids:
        return [dict(row) for row in records]
    return [
        dict(row)
        for row in records
        if not row.get("sample_id") or row.get("sample_id", "") in matrix_ids
    ]


def _scope_records(
    records: Sequence[Mapping[str, str]],
    scope: ScopeSpec,
) -> list[tuple[dict[str, str], list[dict[str, str]]]]:
    rows = [dict(row) for row in records]
    if scope.mode == "all":
        return [({}, rows)]
    if scope.mode == "fixed":
        selected = [
            row for row in rows
            if all(row.get(field, "").strip() == value for field, value in scope.fixed_filters.items())
        ]
        return [(dict(scope.fixed_filters), selected)]
    groups: dict[tuple[str, ...], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        key = tuple(row.get(field, "").strip() for field in scope.blocking_fields)
        if all(key):
            groups[key].append(row)
    return [
        (dict(zip(scope.blocking_fields, key)), groups[key])
        for key in sorted(groups, key=lambda item: tuple(part.casefold() for part in item))
    ]


def _scalar_params(params: Mapping[str, Any]) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in params.items()
        if value is None or isinstance(value, (str, int, float, bool))
    }


def _job_artifact_names(job: JobRecord) -> list[str]:
    names: set[str] = set()
    for item in [*job.artifacts, *job.result_files]:
        for name in (getattr(item, "path", None), getattr(item, "filename", None)):
            if name:
                names.add(str(name))
    return sorted(names)


def _unique_artifacts(job: JobRecord) -> list[FileArtifactInfo]:
    result: list[FileArtifactInfo] = []
    seen: set[str] = set()
    for item in [*job.artifacts, *job.result_files]:
        if item.path in seen:
            continue
        seen.add(item.path)
        result.append(item)
    return sorted(result, key=lambda item: item.path)


def _infer_artifact_schema(format_name: str, text: str) -> ArtifactSchema:
    if format_name == "csv":
        try:
            rows = csv.reader(io.StringIO(text))
            columns = [str(value).strip() for value in next(rows, []) if str(value).strip()]
            sample = next(rows, [])
        except csv.Error:
            return ArtifactSchema(format="csv")
        types = {
            column: _infer_scalar_type(sample[index] if index < len(sample) else None)
            for index, column in enumerate(columns)
        }
        return ArtifactSchema(format="csv", columns=columns, column_types=types)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return ArtifactSchema(format="json")
    row: Mapping[str, Any] | None = None
    if isinstance(payload, Mapping):
        row = payload
    elif isinstance(payload, list) and payload and isinstance(payload[0], Mapping):
        row = payload[0]
    if row is None:
        return ArtifactSchema(format="json")
    columns = [str(key) for key, value in row.items() if _is_json_scalar(value)]
    types = {column: _infer_scalar_type(row.get(column)) for column in columns}
    return ArtifactSchema(format="json", columns=columns, column_types=types)


def _infer_scalar_type(value: Any) -> str:
    if value is None or value == "":
        return "unknown"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int) and not isinstance(value, bool):
        return "integer"
    if isinstance(value, float):
        return "number"
    text = str(value).strip()
    try:
        float(text)
    except ValueError:
        return "string"
    return "number"


def _dict_rows(content: bytes) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig", errors="replace")))
    return [
        {str(key).strip(): (value or "").strip() for key, value in row.items() if key is not None}
        for row in reader
    ]


def _tool_result(
    tool: ToolName,
    *,
    rows: list[dict[str, Any]],
    ok: bool = True,
    artifact: str | None = None,
    checksum: str | None = None,
    filters: Mapping[str, Any] | None = None,
    sort: str | None = None,
    error_code: str | None = None,
) -> ToolResult:
    trimmed = rows[:MAX_TOOL_ROWS]
    while True:
        result = ToolResult(
            tool=tool,
            ok=ok,
            rows=trimmed,
            truncated=len(trimmed) < len(rows),
            row_count=len(rows),
            artifact=artifact,
            checksum=checksum,
            filters=dict(filters or {}),
            sort=sort,
            error_code=error_code,
        )
        if not trimmed or len(result.model_dump_json().encode("utf-8")) <= MAX_TOOL_BYTES:
            return result
        trimmed.pop()


def _allowed_result_artifact(artifact: str, job_type: AnalysisType | None) -> bool:
    name = Path(artifact).name
    if name != artifact or "/" in artifact or "\\" in artifact:
        return False
    differential = name in {"differential_gene_counts.csv", "union_significant_genes.csv"} or name.endswith((".sig.csv", ".all.csv"))
    dem = name in {"differential_metabolite_counts.csv", "union_significant_metabolites.csv"} or name.endswith((".sig.csv", ".all.csv"))
    correlation = name.startswith(("T01_", "T02_", "T03_", "T04_", "T05_", "T06_")) and name.endswith(".csv")
    figure_json = any(name in allowed for allowed in _FIGURE_JSON_BY_ANALYSIS.values())
    if job_type is None:
        return differential or dem or correlation or figure_json
    if job_type is AnalysisType.DEG:
        return differential or name in _FIGURE_JSON_BY_ANALYSIS[job_type]
    if job_type is AnalysisType.DEM:
        return dem or name in _FIGURE_JSON_BY_ANALYSIS[job_type]
    return correlation or name in _FIGURE_JSON_BY_ANALYSIS[job_type]


def _query_json_evidence(
    *,
    text: str,
    artifact: str,
    checksum: str | None,
    field_path: str | None,
    filters: Mapping[str, Any] | None,
    sort: str | None,
    limit: int | None,
    resolve_entity: str | None,
    result_tool: ToolName = ToolName.QUERY_RESULT_EVIDENCE,
) -> ToolResult:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return _tool_result(result_tool, rows=[], ok=False, error_code="invalid_json")
    if not _valid_json_filters(filters):
        return _tool_result(result_tool, rows=[], ok=False, error_code="invalid_filter")
    try:
        rows = _json_evidence_rows(payload, field_path)
    except (KeyError, IndexError, TypeError, ValueError):
        return _tool_result(result_tool, rows=[], ok=False, error_code="invalid_field")

    effective_filters = dict(filters or {})
    rows = [
        row for row in rows
        if all(str(row.get(key, "")) == str(value) for key, value in effective_filters.items())
    ]
    if resolve_entity:
        rows = [row for row in rows if _json_row_matches_entity(row, resolve_entity)]
    if sort:
        sort_field, _, direction = sort.partition(" ")
        reverse = direction.strip().lower() == "desc"
        if not all(sort_field in row for row in rows):
            return _tool_result(result_tool, rows=[], ok=False, error_code="invalid_sort")
        rows.sort(key=lambda row: _sort_value(row.get(sort_field)), reverse=reverse)

    max_rows = min(MAX_TOOL_ROWS, max(1, int(limit or MAX_TOOL_ROWS)))
    selected = rows[:max_rows]
    result = _tool_result(
        result_tool,
        rows=selected,
        artifact=artifact,
        checksum=checksum or "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest(),
        filters=effective_filters,
        sort=sort,
    )
    result.row_count = len(rows)
    result.truncated = result.truncated or len(selected) < len(rows)
    return result


def _json_evidence_rows(payload: Any, field_path: str | None) -> list[dict[str, Any]]:
    parts = field_path.split(".") if field_path else []
    if len(parts) > 12 or any(not part or not _JSON_PATH_PART.fullmatch(part) for part in parts):
        raise ValueError("invalid JSON field path")
    selected = payload
    for part in parts:
        if isinstance(selected, Mapping):
            selected = selected[part]
        elif isinstance(selected, list) and part.isdigit():
            selected = selected[int(part)]
        else:
            raise TypeError("JSON field path does not resolve")

    if isinstance(selected, list):
        return [
            _json_row(item, row_id=index + 1)
            for index, item in enumerate(selected)
        ]
    return [_json_row(selected, row_id=1)]


def _json_row(value: Any, *, row_id: int) -> dict[str, Any]:
    row: dict[str, Any] = {"_row_id": row_id}
    if isinstance(value, Mapping):
        row.update({str(key): item for key, item in value.items() if _is_json_scalar(item)})
        if len(row) == 1:
            raise TypeError("selected JSON object has no scalar fields")
    elif _is_json_scalar(value):
        row["value"] = value
    else:
        raise TypeError("selected JSON value is not a scalar or object row")
    return row


def _is_json_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _valid_json_filters(filters: Mapping[str, Any] | None) -> bool:
    if filters is None:
        return True
    return (
        len(filters) <= 8
        and all(isinstance(key, str) and 0 < len(key) <= 100 for key in filters)
        and all(_is_json_scalar(value) for value in filters.values())
    )


def _json_row_matches_entity(row: Mapping[str, Any], entity: str) -> bool:
    lowered = {str(key).lower(): value for key, value in row.items()}
    return any(str(lowered.get(field, "")) == entity for field in _JSON_ENTITY_FIELDS)


def _inspect_input(field: str, item: AgentInputFile) -> dict[str, Any]:
    parsed = list(csv.reader(io.StringIO(item.content.decode("utf-8-sig", errors="replace"))))
    headers = parsed[0] if parsed else []
    data_rows = parsed[1:]
    column_count = len(headers)
    columns = headers if column_count <= 12 else headers[:10] + headers[-2:]
    row = {
        "field": field,
        "filename": item.filename,
        "columns": columns,
        "column_count": column_count,
        "row_count": len(data_rows),
        "size_bytes": len(item.content),
    }
    if field in {"counts", "metabs", "transcriptome", "metabolome"}:
        values = [cell.strip() for cells in data_rows for cell in cells[1:]]
        present = [cell for cell in values if cell]
        feature_ids = [
            cells[0].strip()
            for cells in data_rows
            if cells and cells[0].strip()
        ]
        feature_id_total = len(feature_ids)
        sample_positions = list(range(min(10, feature_id_total)))
        for position in (
            feature_id_total // 4,
            feature_id_total // 2,
            (3 * feature_id_total) // 4,
            (7 * feature_id_total) // 8,
            feature_id_total - 1,
        ):
            if 0 <= position < feature_id_total and position not in sample_positions:
                sample_positions.append(position)
        sample_positions.sort()
        feature_id_sample = [feature_ids[position][:48] for position in sample_positions]
        numeric: list[float] = []
        for cell in present:
            try:
                numeric.append(float(cell))
            except ValueError:
                continue
        row.update({
            "dtype": "numeric" if len(numeric) == len(present) else "mixed",
            "min": min(numeric) if numeric else None,
            "max": max(numeric) if numeric else None,
            "has_negative": any(value < 0 for value in numeric),
            "integer_ratio": (sum(value.is_integer() for value in numeric) / len(numeric)) if numeric else 0.0,
            "missing_rate": ((len(values) - len(present)) / len(values)) if values else 0.0,
            "feature_id_sample": feature_id_sample,
            "feature_id_total": feature_id_total,
        })
    else:
        records = _dict_rows(item.content)
        group_replicates: dict[str, dict[str, int]] = {}
        for header in headers:
            if header == "sample_id":
                continue
            counts: dict[str, int] = {}
            for record in records:
                value = record.get(header, "")
                counts[value] = counts.get(value, 0) + 1
            group_replicates[header] = counts
        row["group_replicates"] = group_replicates
        if field in {"metadata", "group"} and len(data_rows) <= 60 and column_count <= 10:
            row["raw_rows"] = [
                [str(cell).strip()[:60] for cell in cells[:10]]
                for cells in data_rows[:60]
            ]
    return row


def _sort_value(value: str | None) -> tuple[int, object]:
    if value is None:
        return (0, "")
    try:
        return (1, float(value))
    except (TypeError, ValueError):
        return (2, str(value))
