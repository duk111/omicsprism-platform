from __future__ import annotations

import csv
import io
import math
from dataclasses import dataclass, field
from typing import Iterable

from fastapi import UploadFile

from .models import (
    AnalysisType,
    JobParams,
    PreflightFileSummary,
    PreflightIssue,
    PreflightIssueCode,
    PreflightResponse,
)


@dataclass
class MatrixProfile:
    field: str
    filename: str
    headers: list[str] = field(default_factory=list)
    row_count: int = 0
    sample_names: list[str] = field(default_factory=list)
    feature_ids: list[str] = field(default_factory=list)
    duplicate_feature_ids: list[str] = field(default_factory=list)
    empty_sample_columns: list[str] = field(default_factory=list)
    empty_feature_ids: int = 0
    non_numeric_cells: int = 0
    negative_values: int = 0
    non_integer_values: int = 0
    non_finite_values: int = 0
    row_length_issues: int = 0
    parse_error: str | None = None


@dataclass
class GroupProfile:
    field: str
    filename: str
    headers: list[str] = field(default_factory=list)
    rows: list[dict[str, str]] = field(default_factory=list)
    sample_ids: list[str] = field(default_factory=list)
    duplicate_sample_ids: list[str] = field(default_factory=list)
    empty_columns: list[str] = field(default_factory=list)
    row_length_issues: int = 0
    parse_error: str | None = None


@dataclass(frozen=True)
class ContrastPreview:
    compare_field: str
    tested_level: str
    reference_level: str
    same_fields: tuple[str, ...]
    same_values: dict[str, str]
    tested_count: int
    reference_count: int

    def as_dict(self) -> dict[str, object]:
        return {
            "compare_field": self.compare_field,
            "tested_level": self.tested_level,
            "reference_level": self.reference_level,
            "same_fields": list(self.same_fields),
            "same_values": self.same_values,
            "tested_count": self.tested_count,
            "reference_count": self.reference_count,
        }


def build_contrast_preview(
    metadata_rows: Iterable[dict[str, str]],
    params: JobParams,
) -> tuple[list[ContrastPreview], list[PreflightIssue]]:
    """从 metadata 确定性生成 contrast；无完整 tested/reference 时不返回可提交 contrast。"""
    rows = list(metadata_rows)
    compare_field = str(params.get("compare_field") or "").strip()
    reference_level = str(params.get("reference_level") or "").strip()
    tested_levels = [item.strip() for item in str(params.get("tested_levels") or "").split(",") if item.strip()]
    same_fields = tuple(item.strip() for item in str(params.get("same_fields") or "").split(",") if item.strip())
    try:
        min_replicates = max(1, int(params.get("min_replicates") or 2))
    except (TypeError, ValueError):
        min_replicates = 2

    issues: list[PreflightIssue] = []
    if not compare_field or not reference_level or not tested_levels:
        issues.append(PreflightIssue(
            code=PreflightIssueCode.MISSING_REQUIRED_FIELD,
            field="contrast",
            message="compare_field, tested_levels and reference_level are required",
            suggestions=["Provide both tested and reference levels before submitting."],
        ))
        return [], issues
    if compare_field in same_fields:
        issues.append(PreflightIssue(
            code=PreflightIssueCode.GROUP_SCHEMA_INVALID,
            field="same_fields",
            message="same_fields must not contain compare_field",
            context={"compare_field": compare_field, "same_fields": list(same_fields)},
            suggestions=["Remove compare_field from same_fields so the contrast can be formed."],
        ))
        return [], issues

    columns = set(rows[0]) if rows else set()
    if compare_field not in columns:
        issues.append(PreflightIssue(
            code=PreflightIssueCode.MISSING_REQUIRED_COLUMNS,
            field="compare_field",
            message=f"compare_field '{compare_field}' does not exist in metadata",
            context={"columns": sorted(columns)},
            suggestions=["Choose a metadata column containing the experimental groups."],
        ))
        return [], issues
    missing_same = sorted(field for field in same_fields if field not in columns)
    if missing_same:
        issues.append(PreflightIssue(
            code=PreflightIssueCode.MISSING_REQUIRED_COLUMNS,
            field="same_fields",
            message="same_fields contains columns not found in metadata",
            context={"missing": missing_same},
            suggestions=["Use only metadata column names in same_fields."],
        ))
        return [], issues

    grouped_rows: dict[tuple[str, ...], list[dict[str, str]]] = {}
    for row in rows:
        key = tuple(row.get(field, "").strip() for field in same_fields)
        grouped_rows.setdefault(key, []).append(row)

    contrasts: list[ContrastPreview] = []
    rejected_groups: list[dict[str, object]] = []
    for tested_level in tested_levels:
        for same_key, group_rows in grouped_rows.items():
            tested_count = sum(1 for row in group_rows if row.get(compare_field, "").strip() == tested_level)
            reference_count = sum(1 for row in group_rows if row.get(compare_field, "").strip() == reference_level)
            same_values = dict(zip(same_fields, same_key))
            if tested_level == reference_level or tested_count < min_replicates or reference_count < min_replicates:
                rejected_groups.append({
                    "tested_level": tested_level,
                    "reference_level": reference_level,
                    "same_values": same_values,
                    "tested_count": tested_count,
                    "reference_count": reference_count,
                })
                continue
            contrasts.append(ContrastPreview(
                compare_field=compare_field,
                tested_level=tested_level,
                reference_level=reference_level,
                same_fields=same_fields,
                same_values=same_values,
                tested_count=tested_count,
                reference_count=reference_count,
            ))
    if rejected_groups:
        issues.append(PreflightIssue(
            code=PreflightIssueCode.GROUP_SCHEMA_INVALID,
            field="contrast",
            severity="warning" if contrasts else "error",
            message="Some metadata strata do not meet contrast replicate requirements",
            context={"min_replicates": min_replicates, "rejected_groups": rejected_groups},
            suggestions=["Choose distinct levels with enough biological replicates in each same_fields stratum."],
        ))
    return contrasts, issues


class PreflightService:
    def preflight(
        self,
        analysis_type: AnalysisType,
        *,
        params: JobParams,
        files: dict[str, UploadFile],
    ) -> PreflightResponse:
        issues: list[PreflightIssue] = []
        normalized_params = dict(params)

        if analysis_type == AnalysisType.DEG:
            counts = self._load_matrix("counts", files.get("counts"))
            metadata = self._load_group("metadata", files.get("metadata"))
            issues.extend(self._require_fields(analysis_type, files, ["counts", "metadata"]))
            if counts:
                issues.extend(self._validate_matrix(counts, "counts"))
            if metadata:
                issues.extend(self._validate_group(metadata, "metadata"))
                issues.extend(self._validate_deg_params(params, metadata))
            if counts and metadata:
                issues.extend(self._validate_alignment(counts.sample_names, metadata.sample_ids, "counts", "metadata"))
            file_summaries = [self._matrix_summary(counts), self._group_summary(metadata)]
        elif analysis_type == AnalysisType.DEM:
            metabs = self._load_matrix("metabs", files.get("metabs"))
            metadata = self._load_group("metadata", files.get("metadata"))
            issues.extend(self._require_fields(analysis_type, files, ["metabs", "metadata"]))
            if metabs:
                issues.extend(self._validate_matrix(metabs, "metabs"))
            if metadata:
                issues.extend(self._validate_group(metadata, "metadata"))
                issues.extend(self._validate_deg_params(params, metadata))
            if metabs and metadata:
                issues.extend(self._validate_alignment(metabs.sample_names, metadata.sample_ids, "metabs", "metadata"))
            file_summaries = [self._matrix_summary(metabs), self._group_summary(metadata)]
        else:
            transcriptome = self._load_matrix("transcriptome", files.get("transcriptome"))
            metabolome = self._load_matrix("metabolome", files.get("metabolome"))
            group = self._load_group("group", files.get("group"))
            issues.extend(self._require_fields(analysis_type, files, ["transcriptome", "metabolome", "group"]))
            if transcriptome:
                issues.extend(self._validate_matrix(transcriptome, "transcriptome"))
            if metabolome:
                issues.extend(self._validate_matrix(metabolome, "metabolome"))
            if group:
                issues.extend(self._validate_group(group, "group"))
            if transcriptome and metabolome:
                issues.extend(self._validate_alignment(transcriptome.sample_names, metabolome.sample_names, "transcriptome", "metabolome"))
            if group and transcriptome and metabolome:
                issues.extend(self._validate_group_membership(group.sample_ids, transcriptome.sample_names, metabolome.sample_names))
            file_summaries = [self._matrix_summary(transcriptome), self._matrix_summary(metabolome), self._group_summary(group)]

        errors, warnings = self._split_issues(issues)
        return PreflightResponse(
            analysis_type=analysis_type,
            ok=not errors,
            can_submit=not errors,
            normalized_params=normalized_params,
            files=[item for item in file_summaries if item is not None],
            errors=errors,
            warnings=warnings,
        )

    def _load_matrix(self, field: str, upload: UploadFile | None) -> MatrixProfile | None:
        if upload is None:
            return None
        profile = MatrixProfile(field=field, filename=upload.filename or field)
        try:
            rows = list(csv.reader(io.StringIO(self._read_upload_text(upload), newline="")))
            if not rows:
                profile.parse_error = "empty file"
                return profile
            profile.headers = [cell.strip() for cell in rows[0]]
            data_rows = rows[1:]
            profile.row_count = len(data_rows)
            self._scan_matrix(profile, data_rows)
            return profile
        except Exception as exc:
            profile.parse_error = str(exc)
            return profile
        finally:
            try:
                upload.file.seek(0)
            except Exception:
                pass

    def _load_group(self, field: str, upload: UploadFile | None) -> GroupProfile | None:
        if upload is None:
            return None
        profile = GroupProfile(field=field, filename=upload.filename or field)
        try:
            reader = csv.DictReader(io.StringIO(self._read_upload_text(upload), newline=""))
            if reader.fieldnames is None:
                profile.parse_error = "empty file"
                return profile
            profile.headers = [name.strip() for name in reader.fieldnames]
            rows = list(reader)
            profile.rows = rows
            self._scan_group(profile, rows)
            return profile
        except Exception as exc:
            profile.parse_error = str(exc)
            return profile
        finally:
            try:
                upload.file.seek(0)
            except Exception:
                pass

    def _read_upload_text(self, upload: UploadFile) -> str:
        """读取上传内容，不依赖临时文件实现完整的 IOBase 接口。"""
        upload.file.seek(0)
        raw = upload.file.read()
        if isinstance(raw, str):
            return raw
        if not isinstance(raw, (bytes, bytearray)):
            raise TypeError("uploaded file stream must return bytes or text")
        return bytes(raw).decode("utf-8-sig", errors="replace")

    def _scan_matrix(self, profile: MatrixProfile, rows: list[list[str]]) -> None:
        expected = len(profile.headers)
        if expected == 0:
            return

        empty_counts = [0] * expected
        seen: set[str] = set()
        duplicates: set[str] = set()
        empty_feature_ids = 0

        for row in rows:
            if len(row) != expected:
                profile.row_length_issues += 1
            padded = row + [""] * max(0, expected - len(row))
            feature_id = padded[0].strip() if padded else ""
            if feature_id:
                profile.feature_ids.append(feature_id)
                if feature_id in seen:
                    duplicates.add(feature_id)
                seen.add(feature_id)
            else:
                empty_feature_ids += 1
            for index in range(1, expected):
                value = padded[index].strip()
                if not value:
                    empty_counts[index] += 1
                else:
                    num_valid, num_negative, num_non_int, num_non_finite = self._classify_numeric(value)
                    if not num_valid:
                        profile.non_numeric_cells += 1
                    else:
                        if num_negative:
                            profile.negative_values += 1
                        if num_non_int:
                            profile.non_integer_values += 1
                        if num_non_finite:
                            profile.non_finite_values += 1

        profile.duplicate_feature_ids = sorted(duplicates)
        profile.empty_sample_columns = [profile.headers[index] for index, count in enumerate(empty_counts) if index > 0 and count == len(rows)]
        profile.sample_names = [header for header in profile.headers[1:] if header]
        profile.empty_feature_ids = empty_feature_ids

    def _scan_group(self, profile: GroupProfile, rows: list[dict[str, str]]) -> None:
        expected = set(profile.headers)
        seen: set[str] = set()
        duplicates: set[str] = set()
        empty_columns = {name: 0 for name in profile.headers}

        for row in rows:
            values = {key.strip(): (value.strip() if value is not None else "") for key, value in row.items() if key is not None}
            if set(values.keys()) != expected:
                profile.row_length_issues += 1
            sample_id = values.get("sample_id", "")
            if sample_id:
                profile.sample_ids.append(sample_id)
                if sample_id in seen:
                    duplicates.add(sample_id)
                seen.add(sample_id)
            for key in profile.headers:
                if not values.get(key, ""):
                    empty_columns[key] += 1

        profile.duplicate_sample_ids = sorted(duplicates)
        profile.empty_columns = [key for key, count in empty_columns.items() if count == len(rows)]

    def _require_fields(
        self,
        analysis_type: AnalysisType,
        files: dict[str, UploadFile],
        required_fields: list[str],
    ) -> list[PreflightIssue]:
        issues: list[PreflightIssue] = []
        for field_name in required_fields:
            if files.get(field_name) is None:
                issues.append(
                    PreflightIssue(
                        code=PreflightIssueCode.MISSING_REQUIRED_FIELD,
                        field=field_name,
                        message=f"{field_name} is required for {analysis_type.value} analysis",
                        suggestions=[f"Upload the {field_name} CSV before running preflight again."],
                    )
                )
        return issues

    def _validate_matrix(self, profile: MatrixProfile, field_name: str) -> list[PreflightIssue]:
        issues: list[PreflightIssue] = []
        if profile.parse_error:
            issues.append(
                PreflightIssue(
                    code=PreflightIssueCode.INVALID_CSV,
                    field=field_name,
                    message=f"{field_name} is not a readable CSV file",
                    context={"detail": profile.parse_error},
                    suggestions=["Open the file in a spreadsheet editor and export it again as UTF-8 CSV."],
                )
            )
            return issues
        if len(profile.headers) < 2:
            issues.append(
                PreflightIssue(
                    code=PreflightIssueCode.MATRIX_SCHEMA_INVALID,
                    field=field_name,
                    message=f"{field_name} must contain one feature ID column and at least one sample column",
                    context={"columns": profile.headers},
                    suggestions=[
                        "Keep the first column as feature IDs and place one sample per remaining column.",
                        "Check that the header row is present and not empty.",
                    ],
                )
            )
        if profile.row_length_issues:
            issues.append(
                PreflightIssue(
                    code=PreflightIssueCode.INCONSISTENT_ROW_LENGTH,
                    field=field_name,
                    message=f"{field_name} has rows with inconsistent column counts",
                    context={"count": profile.row_length_issues},
                    suggestions=["Check for missing separators, broken quotes, or rows with extra delimiters."],
                )
            )
        if profile.duplicate_feature_ids:
            issues.append(
                PreflightIssue(
                    code=PreflightIssueCode.DUPLICATE_FEATURE_ID,
                    field=field_name,
                    message=f"{field_name} contains duplicated feature IDs",
                    context={"duplicates": profile.duplicate_feature_ids[:20]},
                    suggestions=["Make feature IDs unique or collapse repeated rows before submitting."],
                )
            )
        if profile.empty_sample_columns:
            issues.append(
                PreflightIssue(
                    code=PreflightIssueCode.EMPTY_COLUMN,
                    field=field_name,
                    severity="warning",
                    message=f"{field_name} contains empty sample columns",
                    context={"columns": profile.empty_sample_columns},
                    suggestions=["The analysis package can drop or handle unusable columns, but confirm the samples were exported correctly."],
                )
            )
        if profile.non_numeric_cells:
            issues.append(
                PreflightIssue(
                    code=PreflightIssueCode.NON_NUMERIC_VALUE,
                    field=field_name,
                    severity="warning",
                    message=f"{field_name} contains non-numeric values in expression cells",
                    context={"count": profile.non_numeric_cells},
                    suggestions=["The analysis package can impute supported missing values; review the file if the count is unexpectedly high."],
                )
            )
        if profile.empty_feature_ids:
            issues.append(
                PreflightIssue(
                    code=PreflightIssueCode.MATRIX_SCHEMA_INVALID,
                    field=field_name,
                    message=f"{field_name} contains {profile.empty_feature_ids} empty feature ID(s)",
                    context={"empty_feature_id_count": profile.empty_feature_ids},
                    suggestions=["Ensure every row has a non-empty feature ID in the first column."],
                )
            )
        if field_name == "counts":
            counts_issues = self._validate_counts_values(profile)
            issues.extend(counts_issues)
        return issues

    def _validate_group(self, profile: GroupProfile, field_name: str) -> list[PreflightIssue]:
        issues: list[PreflightIssue] = []
        if profile.parse_error:
            issues.append(
                PreflightIssue(
                    code=PreflightIssueCode.INVALID_CSV,
                    field=field_name,
                    message=f"{field_name} is not a readable CSV file",
                    context={"detail": profile.parse_error},
                    suggestions=["Open the table in a spreadsheet editor and export it again as UTF-8 CSV."],
                )
            )
            return issues
        sample_id_check = (
            "sample_id" in profile.headers
            if field_name == "metadata"
            else "sample_id" in {name.lower() for name in profile.headers}
        )
        if not sample_id_check:
            issues.append(
                PreflightIssue(
                    code=PreflightIssueCode.MISSING_REQUIRED_COLUMNS,
                    field=field_name,
                    message=f"{field_name} must include a sample_id column",
                    context={"missing": ["sample_id"]},
                    suggestions=["Add a sample_id column with one row per sample."],
                )
            )
        if field_name == "group":
            normalized = {name.lower() for name in profile.headers}
            required = {"sample_id", "group1", "group2"}
            missing_group_cols = sorted(required - normalized)
            if missing_group_cols:
                issues.append(
                    PreflightIssue(
                        code=PreflightIssueCode.GROUP_SCHEMA_INVALID,
                        field=field_name,
                        message=f"{field_name} must contain columns: sample_id, group1, group2",
                        context={"columns": profile.headers, "missing": missing_group_cols},
                        suggestions=["Ensure the group table has sample_id, group1, and group2 columns."],
                    )
                )
            elif len(profile.headers) < 3:
                issues.append(
                    PreflightIssue(
                        code=PreflightIssueCode.GROUP_SCHEMA_INVALID,
                        field=field_name,
                        message=f"{field_name} should contain sample_id, group1 and group2 columns",
                        context={"columns": profile.headers},
                        suggestions=["Use sample_id plus at least two grouping columns so samples can be matched and stratified."],
                    )
                )
        if profile.row_length_issues:
            issues.append(
                PreflightIssue(
                    code=PreflightIssueCode.INCONSISTENT_ROW_LENGTH,
                    field=field_name,
                    message=f"{field_name} has rows with inconsistent column counts",
                    context={"count": profile.row_length_issues},
                    suggestions=["Check for missing values, extra commas, or unescaped text in the CSV."],
                )
            )
        if profile.duplicate_sample_ids:
            issues.append(
                PreflightIssue(
                    code=PreflightIssueCode.DUPLICATE_SAMPLE_ID,
                    field=field_name,
                    message=f"{field_name} contains duplicated sample IDs",
                    context={"duplicates": profile.duplicate_sample_ids[:20]},
                    suggestions=["Deduplicate the sample_id column before submitting."],
                )
            )
        if profile.empty_columns:
            issues.append(
                PreflightIssue(
                    code=PreflightIssueCode.EMPTY_COLUMN,
                    field=field_name,
                    severity="warning",
                    message=f"{field_name} contains empty columns",
                    context={"columns": profile.empty_columns},
                    suggestions=["Empty metadata columns are ignored by the analysis unless they are used as required parameters."],
                )
            )
        return issues

    def _validate_alignment(self, left: list[str], right: list[str], left_field: str, right_field: str) -> list[PreflightIssue]:
        issues: list[PreflightIssue] = []
        if set(left) != set(right):
            common = sorted(set(left) & set(right))
            issues.append(
                PreflightIssue(
                    code=PreflightIssueCode.SAMPLE_MISMATCH,
                    field=f"{left_field}/{right_field}",
                    severity="warning",
                    message="Sample names do not match",
                    context={left_field: left, right_field: right, "common_samples": common, "common_sample_count": len(common)},
                    suggestions=[
                        "The analysis package will align samples by name and use the common sample set.",
                        "Check spelling differences or dropped samples if the common sample count is lower than expected.",
                    ],
                )
            )
        elif left != right:
            issues.append(
                PreflightIssue(
                    code=PreflightIssueCode.SAMPLE_ORDER_MISMATCH,
                    field=f"{left_field}/{right_field}",
                    severity="warning",
                    message="Sample order differs",
                    context={left_field: left, right_field: right},
                    suggestions=["The analysis package will align samples by name before running."],
                )
            )
        return issues

    def _validate_deg_params(self, params: JobParams, metadata: GroupProfile) -> list[PreflightIssue]:
        issues: list[PreflightIssue] = []
        compare_field = str(params.get("compare_field") or "").strip()
        same_fields_raw = str(params.get("same_fields") or "").strip()
        same_fields = [s.strip() for s in same_fields_raw.split(",") if s.strip()]
        tested_levels_raw = str(params.get("tested_levels") or "").strip()
        tested_levels = [s.strip() for s in tested_levels_raw.split(",") if s.strip()]
        reference_level = str(params.get("reference_level") or "").strip()

        metadata_cols = set(metadata.headers)
        if compare_field and compare_field not in metadata_cols:
            issues.append(
                PreflightIssue(
                    code=PreflightIssueCode.MISSING_REQUIRED_COLUMNS,
                    field="compare_field",
                    message=f"compare_field '{compare_field}' does not exist in metadata columns",
                    context={"compare_field": compare_field, "metadata_columns": sorted(metadata_cols)},
                    suggestions=["Set compare_field to a column name that exists in the metadata file."],
                )
            )
        missing_same = [f for f in same_fields if f not in metadata_cols]
        if missing_same:
            issues.append(
                PreflightIssue(
                    code=PreflightIssueCode.MISSING_REQUIRED_COLUMNS,
                    field="same_fields",
                    message=f"same_fields contains columns not found in metadata: {missing_same}",
                    context={"missing": missing_same, "metadata_columns": sorted(metadata_cols)},
                    suggestions=["Use only column names that exist in the metadata file, or leave same_fields empty."],
                )
            )
        if tested_levels and compare_field in metadata_cols:
            observed = {row.get(compare_field, "").strip() for row in metadata.rows}
            for level in tested_levels:
                if level not in observed:
                    issues.append(
                        PreflightIssue(
                            code=PreflightIssueCode.MATRIX_SCHEMA_INVALID,
                            field="tested_levels",
                            severity="warning",
                            message=f"tested_level '{level}' does not appear in metadata column '{compare_field}'",
                            context={"tested_level": level, "compare_field": compare_field, "observed_levels": sorted(observed)},
                            suggestions=["No contrast will be generated for this level."],
                        )
                    )
        if reference_level and compare_field in metadata_cols:
            observed = {row.get(compare_field, "").strip() for row in metadata.rows}
            if reference_level not in observed:
                issues.append(
                    PreflightIssue(
                        code=PreflightIssueCode.MATRIX_SCHEMA_INVALID,
                        field="reference_level",
                        message=f"reference_level '{reference_level}' does not appear in metadata column '{compare_field}'",
                        context={"reference_level": reference_level, "compare_field": compare_field, "observed_levels": sorted(observed)},
                        suggestions=["Set reference_level to a value that exists in the compare_field column of metadata."],
                    )
                )
        return issues

    def _validate_group_membership(self, group_sample_ids: list[str], transcriptome_samples: list[str], metabolome_samples: list[str]) -> list[PreflightIssue]:
        issues: list[PreflightIssue] = []
        allowed = set(transcriptome_samples) & set(metabolome_samples)
        missing = sorted(sample_id for sample_id in group_sample_ids if sample_id not in allowed)
        if missing:
            issues.append(
                PreflightIssue(
                    code=PreflightIssueCode.SAMPLE_MISMATCH,
                    field="group",
                    severity="warning",
                    message="Group table samples do not match transcriptome/metabolome samples",
                    context={"mismatched_samples": missing[:50]},
                    suggestions=["The analysis package will use the intersection between group samples and matrix samples."],
                )
            )
        return issues

    def _matrix_summary(self, profile: MatrixProfile | None) -> PreflightFileSummary | None:
        if profile is None:
            return None
        return PreflightFileSummary(
            field=profile.field,
            filename=profile.filename,
            rows=profile.row_count,
            columns=len(profile.headers),
            sample_names=profile.sample_names,
            sample_ids=profile.sample_names,
            feature_ids=profile.feature_ids,
            duplicate_ids=profile.duplicate_feature_ids,
            empty_columns=profile.empty_sample_columns,
            required_columns=[],
            non_numeric_cells=profile.non_numeric_cells,
            row_length_issues=profile.row_length_issues,
        )

    def _group_summary(self, profile: GroupProfile | None) -> PreflightFileSummary | None:
        if profile is None:
            return None
        return PreflightFileSummary(
            field=profile.field,
            filename=profile.filename,
            rows=len(profile.rows),
            columns=len(profile.headers),
            sample_names=[],
            sample_ids=profile.sample_ids,
            feature_ids=[],
            duplicate_ids=profile.duplicate_sample_ids,
            empty_columns=profile.empty_columns,
            required_columns=["sample_id"],
            non_numeric_cells=0,
            row_length_issues=profile.row_length_issues,
        )

    def _split_issues(self, issues: Iterable[PreflightIssue]) -> tuple[list[PreflightIssue], list[PreflightIssue]]:
        errors: list[PreflightIssue] = []
        warnings: list[PreflightIssue] = []
        for issue in issues:
            if issue.severity == "warning":
                warnings.append(issue)
            else:
                errors.append(issue)
        return errors, warnings

    @staticmethod
    def _classify_numeric(value: str) -> tuple[bool, bool, bool, bool]:
        """Return (valid, negative, non_integer, non_finite) for a numeric string."""
        try:
            num = float(value)
        except Exception:
            return False, False, False, False
        negative = num < 0
        non_integer = num != math.floor(num)
        non_finite = not math.isfinite(num)
        return True, negative, non_integer, non_finite

    def _validate_counts_values(self, profile: MatrixProfile) -> list[PreflightIssue]:
        issues: list[PreflightIssue] = []
        if profile.negative_values:
            issues.append(
                PreflightIssue(
                    code=PreflightIssueCode.NON_NUMERIC_VALUE,
                    field=profile.field,
                    message=f"{profile.field} contains {profile.negative_values} negative value(s); raw counts must be non-negative integers",
                    context={"negative_count": profile.negative_values},
                    suggestions=["Ensure all count values are non-negative integers."],
                )
            )
        if profile.non_integer_values:
            issues.append(
                PreflightIssue(
                    code=PreflightIssueCode.NON_NUMERIC_VALUE,
                    field=profile.field,
                    message=f"{profile.field} contains {profile.non_integer_values} non-integer value(s); raw counts must be integers",
                    context={"non_integer_count": profile.non_integer_values},
                    suggestions=["Raw count data should consist of integer values."],
                )
            )
        if profile.non_finite_values:
            issues.append(
                PreflightIssue(
                    code=PreflightIssueCode.NON_NUMERIC_VALUE,
                    field=profile.field,
                    message=f"{profile.field} contains {profile.non_finite_values} non-finite value(s)",
                    context={"non_finite_count": profile.non_finite_values},
                    suggestions=["Remove or replace Inf/NaN values in the counts matrix."],
                )
            )
        return issues
