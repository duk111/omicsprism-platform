from __future__ import annotations

import io

from fastapi import UploadFile

from backend.app.models import AnalysisType
from backend.app.preflight import PreflightService, build_contrast_preview


class SeekReadOnlyStream:
    def __init__(self, payload: bytes) -> None:
        self._stream = io.BytesIO(payload)

    def seek(self, offset: int) -> int:
        return self._stream.seek(offset)

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)


def test_preflight_accepts_upload_stream_without_readable_method() -> None:
    counts = UploadFile(
        filename="counts.csv",
        file=SeekReadOnlyStream(b"gene,s1,s2\ng1,10,12\n"),
    )
    metadata = UploadFile(
        filename="metadata.csv",
        file=SeekReadOnlyStream(
            b"sample_id,treatment\ns1,control\ns2,salt\n"
        ),
    )

    result = PreflightService().preflight(
        AnalysisType.DIFFERENTIAL,
        params={
            "compare_field": "treatment",
            "tested_levels": "salt",
            "reference_level": "control",
        },
        files={"counts": counts, "metadata": metadata},
    )

    assert result.files[0].rows == 1
    assert result.files[1].rows == 2
    assert not any(issue.code.value == "invalid_csv" for issue in result.errors)


def test_contrast_preview_requires_tested_reference_and_replicates() -> None:
    rows = [
        {"sample_id": "s1", "treatment": "control", "batch": "b1"},
        {"sample_id": "s2", "treatment": "control", "batch": "b1"},
        {"sample_id": "s3", "treatment": "salt", "batch": "b1"},
        {"sample_id": "s4", "treatment": "salt", "batch": "b1"},
    ]

    contrasts, issues = build_contrast_preview(
        rows,
        {
            "compare_field": "treatment",
            "tested_levels": "salt",
            "reference_level": "control",
            "same_fields": "batch",
            "min_replicates": 2,
        },
    )

    assert not issues
    assert [item.as_dict() for item in contrasts] == [{
        "compare_field": "treatment",
        "tested_level": "salt",
        "reference_level": "control",
        "same_fields": ["batch"],
        "same_values": {"batch": "b1"},
        "tested_count": 2,
        "reference_count": 2,
    }]


def test_contrast_preview_rejects_compare_field_in_same_fields() -> None:
    contrasts, issues = build_contrast_preview(
        [{"sample_id": "s1", "treatment": "salt"}],
        {
            "compare_field": "treatment",
            "tested_levels": "salt",
            "reference_level": "control",
            "same_fields": "treatment",
        },
    )

    assert contrasts == []
    assert any(issue.code.value == "group_schema_invalid" for issue in issues)


def test_contrast_preview_groups_by_same_fields_before_counting_replicates() -> None:
    rows = [
        {"sample_id": "s1", "treatment": "control", "batch": "b1"},
        {"sample_id": "s2", "treatment": "control", "batch": "b1"},
        {"sample_id": "s3", "treatment": "salt", "batch": "b1"},
        {"sample_id": "s4", "treatment": "salt", "batch": "b1"},
        {"sample_id": "s5", "treatment": "control", "batch": "b2"},
        {"sample_id": "s6", "treatment": "salt", "batch": "b2"},
    ]
    contrasts, issues = build_contrast_preview(rows, {
        "compare_field": "treatment", "tested_levels": "salt", "reference_level": "control",
        "same_fields": "batch", "min_replicates": 2,
    })

    assert [item.same_values for item in contrasts] == [{"batch": "b1"}]
    assert any(issue.severity == "warning" for issue in issues)
