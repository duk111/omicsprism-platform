from __future__ import annotations

import csv
import io
from collections.abc import Mapping
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


MatrixRole = Literal["counts", "metabs", "transcriptome", "metabolome"]
MetadataRole = Literal["metadata", "group"]
NumericType = Literal["integer_counts", "continuous_abundance", "mixed"]
AlignmentStatus = Literal["exact", "subset", "mismatch"]


class MatrixProfile(BaseModel):
    """Bounded facts about a feature-by-sample matrix."""

    model_config = ConfigDict(extra="forbid")

    role: MatrixRole
    shape: tuple[int, int]
    sample_ids: list[str]
    feature_type: Literal["gene", "metabolite"]
    feature_id_examples: list[str] = Field(max_length=15)
    numeric_type: NumericType
    has_negative: bool
    missing_rate: float = Field(ge=0, le=1)


class MetadataProfile(BaseModel):
    """Bounded facts about metadata/group rows and their sample alignment."""

    model_config = ConfigDict(extra="forbid")

    role: MetadataRole
    columns: list[str]
    levels: dict[str, dict[str, int]]
    sample_ids: list[str]
    rows: list[list[str]] | None
    alignment: dict[str, AlignmentStatus]


DatasetProfile = Annotated[Union[MatrixProfile, MetadataProfile], Field(discriminator="role")]


def build_dataset_profiles(
    inputs: Mapping[str, tuple[str, bytes]],
) -> list[DatasetProfile]:
    """Convert the existing bounded inspection facts into typed profiles.

    `_inspect_input` remains the source of numeric statistics, sampling, and
    metadata thresholds. This adapter only adds the explicit profile contract
    and uses headers/rows to calculate the alignment map.
    """

    from .tools import AgentInputFile, _inspect_input

    inspected: list[tuple[str, dict[str, object], list[str], list[str]]] = []
    for field, (filename, content) in inputs.items():
        item = AgentInputFile(filename=filename, content=content)
        row = _inspect_input(field, item)
        parsed = list(csv.reader(io.StringIO(content.decode("utf-8-sig", errors="replace"))))
        headers = [str(value).strip() for value in (parsed[0] if parsed else [])]
        sample_ids = _sample_ids(field, headers, parsed[1:])
        inspected.append((field, row, headers, sample_ids))

    matrix_ids = {
        field: sample_ids
        for field, row, _headers, sample_ids in inspected
        if field in {"counts", "metabs", "transcriptome", "metabolome"}
    }
    profiles: list[DatasetProfile] = []
    for field, row, headers, sample_ids in inspected:
        if field in matrix_ids:
            profiles.append(_matrix_profile(field, row, headers, sample_ids))
        elif field in {"metadata", "group"}:
            profiles.append(_metadata_profile(field, row, sample_ids, matrix_ids))
    return profiles


def _matrix_profile(
    field: str,
    row: Mapping[str, object],
    headers: list[str],
    sample_ids: list[str],
) -> MatrixProfile:
    dtype = str(row.get("dtype") or "mixed")
    integer_ratio = float(row.get("integer_ratio") or 0.0)
    if dtype != "numeric":
        numeric_type: NumericType = "mixed"
    elif field == "counts" and integer_ratio == 1.0:
        numeric_type = "integer_counts"
    else:
        numeric_type = "continuous_abundance"
    feature_type: Literal["gene", "metabolite"] = (
        "metabolite" if field in {"metabs", "metabolome"} else "gene"
    )
    return MatrixProfile(
        role=field,  # type: ignore[arg-type]
        shape=(int(row.get("row_count") or 0), max(0, len(headers) - 1)),
        sample_ids=sample_ids,
        feature_type=feature_type,
        feature_id_examples=[str(item) for item in list(row.get("feature_id_sample") or [])[:15]],
        numeric_type=numeric_type,
        has_negative=bool(row.get("has_negative")),
        missing_rate=float(row.get("missing_rate") or 0.0),
    )


def _metadata_profile(
    field: str,
    row: Mapping[str, object],
    sample_ids: list[str],
    matrix_ids: Mapping[str, list[str]],
) -> MetadataProfile:
    raw_levels = row.get("group_replicates")
    levels: dict[str, dict[str, int]] = {}
    if isinstance(raw_levels, Mapping):
        for column, values in raw_levels.items():
            if not isinstance(values, Mapping):
                continue
            levels[str(column)] = {
                str(value): max(0, int(count)) for value, count in values.items()
            }
    raw_rows = row.get("raw_rows")
    rows = (
        [[str(cell) for cell in raw_row] for raw_row in raw_rows]
        if isinstance(raw_rows, list)
        else None
    )
    alignment = {
        matrix_field: _alignment_status(sample_ids, other_ids)
        for matrix_field, other_ids in matrix_ids.items()
    }
    return MetadataProfile(
        role=field,  # type: ignore[arg-type]
        columns=[str(column) for column in list(row.get("columns") or [])],
        levels=levels,
        sample_ids=sample_ids,
        rows=rows,
        alignment=alignment,
    )


def _sample_ids(field: str, headers: list[str], rows: list[list[str]]) -> list[str]:
    if field in {"counts", "metabs", "transcriptome", "metabolome"}:
        return [value for value in headers[1:] if value]
    if field in {"metadata", "group"} and headers:
        sample_index = headers.index("sample_id") if "sample_id" in headers else -1
        if sample_index >= 0:
            return [
                row[sample_index].strip()
                for row in rows
                if len(row) > sample_index and row[sample_index].strip()
            ]
    return []


def _alignment_status(left: list[str], right: list[str]) -> AlignmentStatus:
    left_set, right_set = set(left), set(right)
    if left_set == right_set:
        return "exact"
    if left_set.issubset(right_set) or right_set.issubset(left_set):
        return "subset"
    return "mismatch"
