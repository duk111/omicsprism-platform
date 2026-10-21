from __future__ import annotations

import io

from fastapi import UploadFile
from fastapi.testclient import TestClient

from backend.app.models import AnalysisType
from backend.app.main import app
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
        AnalysisType.DEG,
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


def _deg_uploads(metadata: bytes, counts: bytes = b"gene,s1,s2,s3,s4\ng1,1,2,3,4\n") -> dict[str, UploadFile]:
    return {
        "counts": UploadFile(filename="counts.csv", file=io.BytesIO(counts)),
        "metadata": UploadFile(filename="metadata.csv", file=io.BytesIO(metadata)),
    }


def _deg_params(**overrides: object) -> dict[str, object]:
    params: dict[str, object] = {
        "compare_field": "treatment",
        "tested_levels": "salt",
        "reference_level": "control",
        "min_replicates": 2,
    }
    params.update(overrides)
    return params


def test_preflight_blocks_empty_sample_intersection() -> None:
    result = PreflightService().preflight(
        AnalysisType.DEG,
        params=_deg_params(),
        files=_deg_uploads(b"sample_id,treatment\nx1,control\nx2,salt\n"),
    )

    assert not result.can_submit
    issue = next(item for item in result.errors if item.code.value == "sample_mismatch")
    assert issue.context["common_sample_count"] == 0


def test_preflight_reports_remaining_samples_for_partial_alignment() -> None:
    result = PreflightService().preflight(
        AnalysisType.DEG,
        params=_deg_params(),
        files=_deg_uploads(
            b"sample_id,treatment\ns1,control\ns2,control\ns3,salt\ns4,salt\n",
            counts=b"gene,s1,s2,s3,s4,s5\ng1,1,2,3,4,5\n",
        ),
    )

    assert result.can_submit
    issue = next(item for item in result.warnings if item.code.value == "sample_mismatch")
    assert issue.context["common_sample_count"] == 4
    assert issue.context["analysis_ready"] is True


def test_preflight_blocks_missing_tested_level_and_no_valid_contrast() -> None:
    result = PreflightService().preflight(
        AnalysisType.DEG,
        params=_deg_params(tested_levels="drought"),
        files=_deg_uploads(
            b"sample_id,treatment\ns1,control\ns2,control\ns3,salt\ns4,salt\n"
        ),
    )

    assert not result.can_submit
    assert any(item.field == "tested_levels" for item in result.errors)


def test_preflight_reuses_contrast_validation_for_same_fields_and_replicates() -> None:
    result = PreflightService().preflight(
        AnalysisType.DEG,
        params=_deg_params(same_fields="treatment", min_replicates=3),
        files=_deg_uploads(
            b"sample_id,treatment\ns1,control\ns2,control\ns3,salt\ns4,salt\n"
        ),
    )

    assert not result.can_submit
    assert any(item.field == "same_fields" for item in result.errors)


def test_preflight_rejects_invalid_deg_parameters() -> None:
    result = PreflightService().preflight(
        AnalysisType.DEG,
        params=_deg_params(padj_cutoff=2, min_replicates=0),
        files=_deg_uploads(
            b"sample_id,treatment\ns1,control\ns2,control\ns3,salt\ns4,salt\n"
        ),
    )

    assert not result.can_submit
    assert {item.field for item in result.errors} >= {"padj_cutoff", "min_replicates"}


def test_preflight_rejects_invalid_dem_parameters() -> None:
    result = PreflightService().preflight(
        AnalysisType.DEM,
        params={
            "compare_field": "treatment", "tested_levels": "salt", "reference_level": "control",
            "min_replicates": 2, "max_missing_fraction": 2, "impute_method": "unsupported",
        },
        files={
            "metabs": UploadFile(filename="metabs.csv", file=io.BytesIO(b"metab,s1,s2,s3,s4\nm1,1,2,3,4\n")),
            "metadata": UploadFile(filename="metadata.csv", file=io.BytesIO(
                b"sample_id,treatment\ns1,control\ns2,control\ns3,salt\ns4,salt\n"
            )),
        },
    )

    assert not result.can_submit
    assert {item.field for item in result.errors} >= {"max_missing_fraction", "impute_method"}


def test_preflight_rejects_unsupported_parameters_and_gma_engine_bounds() -> None:
    deg = PreflightService().preflight(
        AnalysisType.DEG,
        params=_deg_params(normalize=False),
        files=_deg_uploads(b"sample_id,treatment\ns1,control\ns2,control\ns3,salt\ns4,salt\n"),
    )
    gma = PreflightService().preflight(
        AnalysisType.GMA,
        params={"method": "pearson", "max_missing_fraction": 1},
        files={
            "transcriptome": UploadFile(filename="transcriptome.csv", file=io.BytesIO(b"gene,s1,s2\ng1,1,2\n")),
            "metabolome": UploadFile(filename="metabolome.csv", file=io.BytesIO(b"metab,s1,s2\nm1,1,2\n")),
            "group": UploadFile(filename="group.csv", file=io.BytesIO(b"sample_id,group1,group2\ns1,a,x\ns2,b,y\n")),
        },
    )
    assert not deg.can_submit
    assert any(issue.field == "normalize" for issue in deg.errors)
    assert not gma.can_submit
    assert {issue.field for issue in gma.errors} >= {"method", "max_missing_fraction"}


def test_preflight_accepts_normal_dem_and_gma_inputs() -> None:
    dem = PreflightService().preflight(
        AnalysisType.DEM,
        params={"compare_field": "treatment", "tested_levels": "salt", "reference_level": "control", "min_replicates": 2},
        files={
            "metabs": UploadFile(filename="metabs.csv", file=io.BytesIO(b"metab,s1,s2,s3,s4\nm1,1,2,3,4\n")),
            "metadata": UploadFile(filename="metadata.csv", file=io.BytesIO(
                b"sample_id,treatment\ns1,control\ns2,control\ns3,salt\ns4,salt\n"
            )),
        },
    )
    gma = PreflightService().preflight(
        AnalysisType.GMA,
        params={},
        files={
            "transcriptome": UploadFile(filename="transcriptome.csv", file=io.BytesIO(b"gene,s1,s2\ng1,1,2\n")),
            "metabolome": UploadFile(filename="metabolome.csv", file=io.BytesIO(b"metab,s1,s2\nm1,1,2\n")),
            "group": UploadFile(filename="group.csv", file=io.BytesIO(b"sample_id,group1,group2\ns1,a,x\ns2,b,y\n")),
        },
    )

    assert dem.can_submit
    assert gma.can_submit


def test_preflight_blocks_empty_gma_sample_intersection() -> None:
    result = PreflightService().preflight(
        AnalysisType.GMA,
        params={},
        files={
            "transcriptome": UploadFile(filename="transcriptome.csv", file=io.BytesIO(b"gene,a\ng1,1\n")),
            "metabolome": UploadFile(filename="metabolome.csv", file=io.BytesIO(b"metab,b\nm1,1\n")),
            "group": UploadFile(filename="group.csv", file=io.BytesIO(b"sample_id,group1,group2\na,x,y\n")),
        },
    )

    assert not result.can_submit
    assert any(item.context.get("common_sample_count") == 0 for item in result.errors)


def test_preflight_missing_files_returns_structured_response() -> None:
    response = TestClient(app).post(
        "/api/jobs/preflight",
        data={"analysis_type": "deg"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["can_submit"] is False
    assert {item["field"] for item in payload["errors"]} >= {"counts", "metadata"}


def test_job_creation_rejects_invalid_parameters_before_persisting_job() -> None:
    response = TestClient(app).post(
        "/api/jobs",
        data={
            "analysis_type": "deg",
            "compare_field": "treatment",
            "tested_levels": "salt",
            "reference_level": "control",
            "padj_cutoff": "2",
        },
        files={
            "counts": ("counts.csv", b"gene,s1,s2,s3,s4\ng1,1,2,3,4\n", "text/csv"),
            "metadata": (
                "metadata.csv",
                b"sample_id,treatment\ns1,control\ns2,control\ns3,salt\ns4,salt\n",
                "text/csv",
            ),
        },
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "preflight_failed"
    assert detail["context"]["preflight"]["errors"][0]["field"] == "padj_cutoff"
