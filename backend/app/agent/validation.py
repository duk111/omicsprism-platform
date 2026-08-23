from __future__ import annotations

import csv
import io
from collections.abc import Mapping, Sequence
from hashlib import sha256
from typing import Literal

from fastapi import UploadFile
from pydantic import BaseModel, ConfigDict, Field

from ..models import AnalysisType
from ..preflight import PreflightService, build_contrast_preview
from .dataset_profile import DatasetProfile, MetadataProfile
from .fingerprint import compute_input_fingerprint
from .param_resolver import AnalysisParams, MissingParam, ResolvedRequest


class DatasetRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_id: str = Field(min_length=1)
    owner_id: str = Field(min_length=1)
    role: Literal["counts", "metabs", "transcriptome", "metabolome", "metadata", "group"]
    filename: str = Field(min_length=1)
    checksum: str = Field(pattern=r"^sha256:[0-9a-fA-F]{64}$")
    content: bytes = Field(exclude=True)
    profile: DatasetProfile | None = Field(default=None, exclude=True)


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
    profiles = [ref.profile for ref in dataset_refs if ref.profile is not None]
    fingerprint_owner = next(iter(owner_ids)) if len(owner_ids) == 1 else ""
    fingerprint = compute_input_fingerprint(
        owner_id=fingerprint_owner,
        dataset_refs=dataset_refs,
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
    if not request.missing and request.params is not None:
        analysis_type = _analysis_type(request.params.analysis_type)
        files = {
            ref.role: UploadFile(filename=ref.filename, file=io.BytesIO(ref.content))
            for ref in dataset_refs
        }
        params = request.params.legacy_params()
        response = PreflightService().preflight(analysis_type, params=params, files=files)
        blocking.extend(_issues(response.errors))
        warnings = _issues(response.warnings)
        preview = None
        if analysis_type in {AnalysisType.DEG, AnalysisType.DEM}:
            metadata_ref = next((ref for ref in dataset_refs if ref.role == "metadata"), None)
            rows = _metadata_rows(metadata_ref)
            contrasts, contrast_issues = build_contrast_preview(rows, params)
            blocking.extend(_issues(item for item in contrast_issues if item.severity == "error"))
            warnings.extend(_issues(item for item in contrast_issues if item.severity == "warning"))
            contrasts = _filter_fixed_same_fields(contrasts, request.params.contrast.same_fields if hasattr(request.params, "contrast") else {})
            if contrasts:
                preview = ContrastPreview.model_validate(contrasts[0].as_dict())
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


def _filter_fixed_same_fields(contrasts: Sequence[object], fixed: Mapping[str, str]) -> list[object]:
    fixed_values = {field: value for field, value in fixed.items() if value}
    if not fixed_values:
        return list(contrasts)
    return [
        item for item in contrasts
        if all(item.same_values.get(field) == value for field, value in fixed_values.items())
    ]
