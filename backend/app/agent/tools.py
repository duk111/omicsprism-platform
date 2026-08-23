from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from pathlib import Path
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..models import AnalysisType, JobRecord
from .dataset_profile import DatasetProfile, build_dataset_profiles
from .schemas import ToolName, ToolResult


MAX_TOOL_ROWS = 50
MAX_TOOL_BYTES = 32 * 1024

_FIGURE_JSON_BY_ANALYSIS = {
    AnalysisType.DIFFERENTIAL: frozenset({"volcano.json"}),
    AnalysisType.DEM: frozenset({"volcano.json"}),
    AnalysisType.CORRELATION: frozenset({
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
        if self.job_store is None or self.files is None:
            raise ToolConfigurationError("result services are not configured")
        if not _allowed_result_artifact(artifact, job_type=None):
            return _tool_result(ToolName.QUERY_RESULT_EVIDENCE, rows=[], ok=False, error_code="artifact_not_allowed")
        job = self.job_store.get_for_user(job_id, self.user_id)
        if not _allowed_result_artifact(artifact, job_type=job.analysis_type):
            return _tool_result(ToolName.QUERY_RESULT_EVIDENCE, rows=[], ok=False, error_code="artifact_not_allowed")
        artifact_info = next(
            (item for item in [*job.artifacts, *job.result_files]
             if item.path == artifact or item.filename == artifact or getattr(item, "name", None) == artifact),
            None,
        )
        if artifact_info is None:
            return _tool_result(ToolName.QUERY_RESULT_EVIDENCE, rows=[], ok=False, error_code="not_found")
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
                return _tool_result(ToolName.QUERY_RESULT_EVIDENCE, rows=[], ok=False, error_code="invalid_sort")
            rows.sort(key=lambda row: _sort_value(row.get(sort_field)), reverse=reverse)
        max_rows = min(MAX_TOOL_ROWS, max(1, int(limit or MAX_TOOL_ROWS)))
        selected = rows[:max_rows]
        result = _tool_result(
            ToolName.QUERY_RESULT_EVIDENCE,
            rows=selected,
            artifact=artifact_info.path,
            checksum=artifact_info.checksum or "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest(),
            filters=effective_filters,
            sort=sort,
        )
        result.row_count = len(rows)
        result.truncated = result.truncated or len(selected) < len(rows)
        return result


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
    if job_type is AnalysisType.DIFFERENTIAL:
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
) -> ToolResult:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return _tool_result(ToolName.QUERY_RESULT_EVIDENCE, rows=[], ok=False, error_code="invalid_json")
    if not _valid_json_filters(filters):
        return _tool_result(ToolName.QUERY_RESULT_EVIDENCE, rows=[], ok=False, error_code="invalid_filter")
    try:
        rows = _json_evidence_rows(payload, field_path)
    except (KeyError, IndexError, TypeError, ValueError):
        return _tool_result(ToolName.QUERY_RESULT_EVIDENCE, rows=[], ok=False, error_code="invalid_field")

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
            return _tool_result(ToolName.QUERY_RESULT_EVIDENCE, rows=[], ok=False, error_code="invalid_sort")
        rows.sort(key=lambda row: _sort_value(row.get(sort_field)), reverse=reverse)

    max_rows = min(MAX_TOOL_ROWS, max(1, int(limit or MAX_TOOL_ROWS)))
    selected = rows[:max_rows]
    result = _tool_result(
        ToolName.QUERY_RESULT_EVIDENCE,
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
