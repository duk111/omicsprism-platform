from datetime import datetime, timezone

import pytest

from backend.app import job_execution
from backend.app.models import AnalysisType, JobRecord, JobStatus


def _job(params: dict[str, object], analysis_type: AnalysisType = AnalysisType.DEM) -> JobRecord:
    now = datetime.now(timezone.utc)
    return JobRecord(
        id="job-params",
        project_name="parameter-test",
        analysis_type=analysis_type,
        status=JobStatus.RUNNING,
        created_at=now,
        updated_at=now,
        params=params,
    )


def _capture_dem_params(monkeypatch: pytest.MonkeyPatch, tmp_path, params: dict[str, object]):
    captured: dict[str, object] = {}

    def fake_pipeline(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(job_execution, "run_dem_pipeline", fake_pipeline)
    monkeypatch.setattr(job_execution, "_generate_dem_figure_data", lambda *_args: None)
    monkeypatch.setattr(job_execution, "_zip_directory", lambda *_args: tmp_path / "results.zip")
    job_execution._run_dem_job(
        _job(params),
        object(),
        tmp_path,
        tmp_path,
        {"metabs": tmp_path / "metabs.csv", "metadata": tmp_path / "metadata.csv"},
        lambda *_args: None,
    )
    return captured


def test_deg_runner_preserves_legal_zero_values(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    def fake_pipeline(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(job_execution, "run_deg_pipeline", fake_pipeline)
    monkeypatch.setattr(job_execution, "_generate_deg_figure_data", lambda *_args: None)
    monkeypatch.setattr(job_execution, "_zip_directory", lambda *_args: tmp_path / "results.zip")
    job_execution._run_differential_job(
        _job({
            "compare_field": "treatment",
            "tested_levels": "salt",
            "reference_level": "control",
            "padj_cutoff": 0,
            "log2fc_cutoff": 0,
            "min_total_count": 0,
            "min_replicates": 1,
        }, AnalysisType.DEG),
        object(),
        tmp_path,
        tmp_path,
        {"counts": tmp_path / "counts.csv", "metadata": tmp_path / "metadata.csv"},
        lambda *_args: None,
    )

    assert captured["padj_cutoff"] == 0
    assert captured["log2fc_cutoff"] == 0
    assert captured["min_total_count"] == 0
    assert captured["min_replicates"] == 1


def test_dem_runner_preserves_legal_zero_values_and_false_flags(monkeypatch, tmp_path) -> None:
    captured = _capture_dem_params(monkeypatch, tmp_path, {
        "vip_cutoff": 0,
        "padj_cutoff": 0,
        "log2fc_cutoff": 0,
        "pseudocount": 0,
        "max_missing_fraction": 0,
        "min_replicates": 1,
        "normalize": False,
        "log_transform": False,
    })

    assert captured["vip_cutoff"] == 0
    assert captured["padj_cutoff"] == 0
    assert captured["log2fc_cutoff"] == 0
    assert captured["pseudocount"] == 0
    assert captured["max_missing_fraction"] == 0
    assert captured["min_replicates"] == 1
    assert captured["normalize"] is False
    assert captured["log_transform"] is False


def test_dem_runner_uses_existing_defaults_only_for_none(monkeypatch, tmp_path) -> None:
    captured = _capture_dem_params(monkeypatch, tmp_path, {
        "vip_cutoff": None,
        "padj_cutoff": None,
        "log2fc_cutoff": None,
        "pseudocount": None,
        "max_missing_fraction": None,
        "min_replicates": None,
        "n_orthogonal_components": None,
        "normalize": False,
        "log_transform": False,
    })

    assert captured["vip_cutoff"] == 1.0
    assert captured["padj_cutoff"] == 0.05
    assert captured["log2fc_cutoff"] == 1.0
    assert captured["pseudocount"] == 1e-9
    assert captured["max_missing_fraction"] == 0.5
    assert captured["min_replicates"] == 2
    assert captured["n_orthogonal_components"] == 1
    assert captured["normalize"] is False
    assert captured["log_transform"] is False


@pytest.mark.parametrize(
    ("value", "options"),
    [
        (-0.1, {"minimum": 0}),
        (1.1, {"minimum": 0, "maximum": 1}),
        (-1, {"minimum": 0, "integer": True}),
        (1.5, {"minimum": 1, "integer": True}),
        (float("nan"), {"minimum": 0}),
        (float("inf"), {"minimum": 0}),
    ],
)
def test_numeric_param_rejects_values_outside_declared_range(value, options) -> None:
    with pytest.raises(ValueError):
        job_execution._numeric_param({"value": value}, "value", 1, **options)


def test_numeric_param_keeps_existing_default_for_none() -> None:
    assert job_execution._numeric_param({"value": None}, "value", 0.05, minimum=0, maximum=1) == 0.05


def test_dem_runner_preserves_zero_orthogonal_components(monkeypatch, tmp_path) -> None:
    captured = _capture_dem_params(monkeypatch, tmp_path, {"n_orthogonal_components": 0})
    assert captured["n_orthogonal_components"] == 0


def test_dem_runner_rejects_negative_component_count_before_pipeline(monkeypatch, tmp_path) -> None:
    pipeline_called = False

    def fake_pipeline(**_kwargs):
        nonlocal pipeline_called
        pipeline_called = True

    monkeypatch.setattr(job_execution, "run_dem_pipeline", fake_pipeline)
    with pytest.raises(ValueError, match="n_orthogonal_components"):
        _capture_dem_params(monkeypatch, tmp_path, {"n_orthogonal_components": -1})
    assert not pipeline_called

