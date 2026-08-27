from __future__ import annotations

import csv
import io
from collections.abc import Sequence
from hashlib import sha256
from typing import Literal

from fastapi import UploadFile
from pydantic import BaseModel, ConfigDict, Field

from ..models import AnalysisType
from ..preflight import PreflightService, build_contrast_preview
from .dataset_profile import DatasetProfile, build_dataset_profiles
from .fingerprint import compute_input_fingerprint
from .param_resolver import AnalysisParams, MissingParam, ResolvedRequest, ScopeSpec


class DatasetRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_id: str = Field(min_length=1)
    owner_id: str = Field(min_length=1)
    role: Literal["counts", "metabs", "transcriptome", "metabolome", "metadata", "group"]
    filename: str = Field(min_length=1)
    checksum: str = Field(pattern=r"^sha256:[0-9a-fA-F]{64}$")
    content: bytes = Field(exclude=True)
    profile: DatasetProfile | None = Field(default=None, exclude=True)


def derive_scoped_dataset_refs(
    scope: ScopeSpec,
    dataset_refs: list[DatasetRef],
) -> list[DatasetRef]:
    """Materialize fixed sample filters before a Job is submitted.

    Blocking scopes deliberately keep the original datasets so the analysis
    engine can create one contrast per blocking stratum. Fixed scopes instead
    rewrite metadata and matrix columns to the selected sample set; this keeps
    the confirmation preview and the executed input identical.
    """

    if scope.mode != "fixed":
        return [item.model_copy(deep=True) for item in dataset_refs]
    metadata = next(
        (item for item in dataset_refs if item.role in {"metadata", "group"}),
        None,
    )
    if metadata is None:
        raise ValueError("fixed scope requires metadata")
    metadata_rows = _csv_rows(metadata.content)
    if not metadata_rows:
        raise ValueError("fixed scope metadata is empty")
    columns = set(metadata_rows[0])
    if "sample_id" not in columns:
        raise ValueError("fixed scope metadata requires a sample_id column")
    missing = sorted(set(scope.fixed_filters) - columns)
    if missing:
        raise ValueError("fixed scope fields are missing from metadata: " + ", ".join(missing))
    selected_rows = [
        row for row in metadata_rows
        if all(row.get(field, "").strip() == value for field, value in scope.fixed_filters.items())
    ]
    sample_ids = [row.get("sample_id", "").strip() for row in selected_rows]
    sample_ids = [item for item in sample_ids if item]
    if not sample_ids:
        raise ValueError("fixed scope selected no samples")
    selected = set(sample_ids)
    result: list[DatasetRef] = []
    derived_inputs: dict[str, tuple[str, bytes]] = {}
    for ref in dataset_refs:
        if ref.role in {"metadata", "group"}:
            content = _csv_bytes(list(metadata_rows[0]), selected_rows)
        elif ref.role in {"counts", "metabs", "transcriptome", "metabolome"}:
            content = _subset_matrix_columns(ref.content, selected)
        else:
            content = ref.content
        if ref.role in {"counts", "metabs", "transcriptome", "metabolome", "metadata", "group"}:
            derived_inputs[ref.role] = (ref.filename, content)
        result.append(ref.model_copy(update={
            "content": content,
            "checksum": "sha256:" + sha256(content).hexdigest(),
        }))
    profiles = {profile.role: profile for profile in build_dataset_profiles(derived_inputs)}
    return [item.model_copy(update={"profile": profiles.get(item.role)}) for item in result]


def _csv_rows(content: bytes) -> list[dict[str, str]]:
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    return [
        {str(key).strip(): str(value or "").strip() for key, value in row.items() if key is not None}
        for row in reader
    ]

def _csv_bytes(columns: list[str], rows: list[dict[str, str]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerows({column: row.get(column, "") for column in columns} for row in rows)
    return output.getvalue().encode("utf-8")


def _subset_matrix_columns(content: bytes, selected_sample_ids: set[str]) -> bytes:
    text = content.decode("utf-8-sig", errors="replace")
    rows = list(csv.reader(io.StringIO(text, newline="")))
    if not rows or not rows[0]:
        raise ValueError("matrix input is empty")
    header = [str(item).strip() for item in rows[0]]
    selected_indexes = [
        index for index, sample_id in enumerate(header[1:], start=1)
        if sample_id in selected_sample_ids
    ]
    if not selected_indexes:
        raise ValueError("fixed scope selected no matrix samples")
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow([header[0], *(header[index] for index in selected_indexes)])
    for row in rows[1:]:
        writer.writerow([row[0] if row else "", *(row[index] if index < len(row) else "" for index in selected_indexes)])
    return output.getvalue().encode("utf-8")


class Issue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    field: str | None = None


class ContrastPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    compare_field: str
    tested_level: str
    reference_level: str
    scope: ScopeSpec = Field(default_factory=lambda: ScopeSpec(mode="all"))
    # Kept as a display-compatible projection for the current API consumer.
    same_fields: list[str] = Field(default_factory=list)
    same_values: dict[str, str] = Field(default_factory=dict)
    tested_count: int = Field(ge=0)
    reference_count: int = Field(ge=0)


class ValidationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    resolved_params: AnalysisParams | None
    blocking: list[Issue] = Field(default_factory=list)
    warnings: list[Issue] = Field(default_factory=list)
    missing: list[MissingParam] = Field(default_factory=list)
    preview: ContrastPreview | None = None
    input_fingerprint: str


def validate_analysis_request(
    request: ResolvedRequest,
    dataset_refs: list[DatasetRef],
) -> ValidationReport:
    """Thin typed adapter over the existing deterministic PreflightService."""

    owner_ids = {ref.owner_id for ref in dataset_refs}
    scoped_refs = dataset_refs
    scope = (
        request.params.contrast.scope
        if request.params is not None and hasattr(request.params, "contrast")
        else None
    )
    scope_error: ValueError | None = None
    if scope is not None:
        try:
            scoped_refs = derive_scoped_dataset_refs(scope, dataset_refs)
        except ValueError as exc:
            scope_error = exc
    profiles = [ref.profile for ref in scoped_refs if ref.profile is not None]
    fingerprint_owner = next(iter(owner_ids)) if len(owner_ids) == 1 else ""
    fingerprint = compute_input_fingerprint(
        owner_id=fingerprint_owner,
        dataset_refs=scoped_refs,
        profiles=profiles,
    )
    blocking: list[Issue] = []
    if not dataset_refs:
        blocking.append(Issue(code="missing_dataset", message="未提供待验证的数据集"))
    if len(owner_ids) > 1:
        blocking.append(Issue(code="ownership_mismatch", message="数据集不属于同一用户"))
    duplicate_roles = sorted(
        role for role in {ref.role for ref in dataset_refs}
        if sum(ref.role == role for ref in dataset_refs) > 1
    )
    if duplicate_roles:
        blocking.append(Issue(
            code="duplicate_dataset_role",
            message=f"输入包含重复的数据集角色：{', '.join(duplicate_roles)}",
            field="dataset_refs",
        ))
    for ref in dataset_refs:
        actual_checksum = "sha256:" + sha256(ref.content).hexdigest()
        if actual_checksum.casefold() != ref.checksum.casefold():
            blocking.append(Issue(
                code="checksum_mismatch",
                message=f"数据集 {ref.dataset_id} 的内容与 checksum 不一致",
                field=ref.role,
            ))
    if request.params is not None and request.analysis_type != request.params.analysis_type:
        blocking.append(Issue(
            code="analysis_type_mismatch",
            message="resolved request 的分析类型与参数类型不一致",
            field="analysis_type",
        ))
    if scope_error is not None:
        blocking.append(Issue(code="invalid_scope", message=str(scope_error), field="scope"))
    if not request.missing and request.params is not None:
        analysis_type = _analysis_type(request.params.analysis_type)
        validation_refs = scoped_refs if scope_error is None else dataset_refs
        files = {
            ref.role: UploadFile(filename=ref.filename, file=io.BytesIO(ref.content))
            for ref in validation_refs
        }
        params = request.params.legacy_params()
        response = PreflightService().preflight(analysis_type, params=params, files=files)
        blocking.extend(_issues(response.errors))
        warnings = _issues(response.warnings)
        preview = None
        if analysis_type in {AnalysisType.DEG, AnalysisType.DEM}:
            metadata_ref = next((ref for ref in validation_refs if ref.role == "metadata"), None)
            rows = _metadata_rows(metadata_ref)
            contrasts, contrast_issues = build_contrast_preview(rows, params)
            blocking.extend(_issues(item for item in contrast_issues if item.severity == "error"))
            warnings.extend(_issues(item for item in contrast_issues if item.severity == "warning"))
            if contrasts:
                preview_data = contrasts[0].as_dict()
                preview_data["scope"] = scope.model_dump(mode="python")
                preview = ContrastPreview.model_validate(preview_data)
            elif metadata_ref is not None and not blocking:
                blocking.append(Issue(code="contrast_unavailable", message="无法从 metadata 形成合法 contrast", field="contrast"))
        return ValidationReport(
            ok=not blocking,
            resolved_params=request.params,
            blocking=blocking,
            warnings=warnings,
            missing=list(request.missing),
            preview=preview,
            input_fingerprint=fingerprint,
        )
    blocking.extend(Issue(code="missing_parameter", message=item.reason, field=item.field) for item in request.missing)
    return ValidationReport(
        ok=False,
        resolved_params=request.params,
        blocking=blocking,
        warnings=[],
        missing=list(request.missing),
        preview=None,
        input_fingerprint=fingerprint,
    )


def _issues(items: Sequence[object]) -> list[Issue]:
    issues: list[Issue] = []
    for item in items:
        raw_code = getattr(item, "code", "validation_error")
        code = raw_code.value if hasattr(raw_code, "value") else raw_code
        issues.append(Issue(
            code=str(code),
            message=str(getattr(item, "message", item)),
            field=getattr(item, "field", None),
        ))
    return issues


def _analysis_type(value: str | None) -> AnalysisType:
    return {
        "DEG": AnalysisType.DEG,
        "DEM": AnalysisType.DEM,
        "GMA": AnalysisType.GMA,
    }[str(value)]


def _metadata_rows(ref: DatasetRef | None) -> list[dict[str, str]]:
    if ref is None:
        return []
    text = ref.content.decode("utf-8-sig", errors="replace")
    return [
        {str(key).strip(): str(value or "").strip() for key, value in row.items() if key is not None}
        for row in csv.DictReader(io.StringIO(text, newline=""))
    ]
