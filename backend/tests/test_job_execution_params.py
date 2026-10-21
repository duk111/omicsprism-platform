from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app import job_execution
from backend.app.agent.param_resolver import _build_params
from backend.app.main import _build_job_params
from omicsprism.io import load_as_anndata, preprocess_adata
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


def test_gma_runner_maps_supported_parameters_to_core_and_preprocessing(monkeypatch, tmp_path) -> None:
    config_values: dict[str, object] = {}
    preprocess_values: dict[str, object] = {}

    def config_factory(**kwargs):
        config_values.update(kwargs)
        return SimpleNamespace(**kwargs, log_level="INFO", knn_neighbors=5)

    class Engine:
        def __init__(self, adata, cfg):
            self.cfg = cfg

        def run_all(self, *, generate_plots):
            assert generate_plots is True

    monkeypatch.setattr(job_execution, "AnalysisConfig", config_factory)
    monkeypatch.setattr(job_execution, "load_as_anndata", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(
        job_execution,
        "preprocess_adata",
        lambda _adata, **kwargs: preprocess_values.update(kwargs) or object(),
    )
    monkeypatch.setattr(job_execution, "MultiOmicsEngine", Engine)
    monkeypatch.setattr(job_execution, "_zip_directory", lambda *_args: tmp_path / "results.zip")

    job_execution._run_correlation_job(
        _job({
            "fdr_cutoff": 0.2,
            "enable_modules": False,
            "max_missing_fraction": 0.25,
            "trans_log2": False,
            "metab_log2": False,
        }, AnalysisType.GMA),
        object(),
        tmp_path,
        tmp_path,
        {
            "group": tmp_path / "group.csv",
            "transcriptome": tmp_path / "transcriptome.csv",
            "metabolome": tmp_path / "metabolome.csv",
        },
        lambda *_args: None,
    )

    assert config_values["fdr_alpha"] == 0.2
    assert config_values["enable_module_detection"] is False
    assert config_values["missing_feature_threshold"] == 0.25
    assert config_values["trans_log2"] is False
    assert config_values["metab_log2"] is False
    assert preprocess_values == {
        "missing_feature_threshold": 0.25,
        "knn_neighbors": 5,
        "trans_log2": False,
        "metab_log2": False,
    }


def test_gma_runner_uses_platform_defaults_and_rejects_core_invalid_threshold(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    def config_factory(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(**kwargs, log_level="INFO", knn_neighbors=5)

    monkeypatch.setattr(job_execution, "AnalysisConfig", config_factory)
    monkeypatch.setattr(job_execution, "load_as_anndata", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(job_execution, "preprocess_adata", lambda adata, **_kwargs: adata)
    monkeypatch.setattr(job_execution, "MultiOmicsEngine", lambda *_args: SimpleNamespace(run_all=lambda **_kwargs: None))
    monkeypatch.setattr(job_execution, "_zip_directory", lambda *_args: tmp_path / "results.zip")

    job_execution._run_correlation_job(
        _job({}, AnalysisType.GMA), object(), tmp_path, tmp_path,
        {"group": tmp_path / "group.csv", "transcriptome": tmp_path / "t.csv", "metabolome": tmp_path / "m.csv"},
        lambda *_args: None,
    )
    assert captured["trans_log2"] is True
    assert captured["metab_log2"] is True
    assert captured["fdr_alpha"] == 0.05
    assert captured["enable_module_detection"] is True
    assert captured["missing_feature_threshold"] == 0.5

    with pytest.raises(ValueError, match="max_missing_fraction"):
        job_execution._run_correlation_job(
            _job({"max_missing_fraction": 1}, AnalysisType.GMA), object(), tmp_path, tmp_path,
            {"group": tmp_path / "group.csv", "transcriptome": tmp_path / "t.csv", "metabolome": tmp_path / "m.csv"},
            lambda *_args: None,
        )


def test_agent_parameter_resolution_rejects_unsupported_options_and_keeps_zero_false() -> None:
    deg = _build_params("DEG", {"normalize": False}, None)
    gma_unsupported = _build_params("GMA", {"method": "pearson"}, None)
    gma = _build_params("GMA", {
        "fdr_cutoff": 0.1,
        "enable_modules": False,
        "trans_log2": False,
        "metab_log2": False,
        "max_missing_fraction": 0,
    }, None)

    assert deg.params is None and deg.missing[0].field == "normalize"
    assert gma_unsupported.params is None and gma_unsupported.missing[0].field == "method"
    assert gma.params is not None
    assert gma.legacy_params() == {
        "fdr_cutoff": 0.1,
        "enable_modules": False,
        "trans_log2": False,
        "metab_log2": False,
        "max_missing_fraction": 0,
    }


def test_api_gma_params_survive_job_record_storage_shape() -> None:
    params = _build_job_params(
        atype=AnalysisType.GMA,
        counts=None, metadata=None, metabs=None, transcriptome=None, metabolome=None, group=None,
        compare_field=None, tested_levels=None, reference_level=None,
        padj_cutoff=None, log2fc_cutoff=None, min_total_count=None, min_replicates=None,
        same_fields=None, normalize=None, filter_low_expression=None, method=None,
        fdr_cutoff=0.2, enable_modules=False, vip_cutoff=None, pseudocount=None,
        max_missing_fraction=0, impute_method=None, log_transform=None,
        trans_log2=False, metab_log2=False, n_orthogonal_components=None,
    )
    job = _job(params, AnalysisType.GMA)
    restored = JobRecord.model_validate_json(job.model_dump_json())

    assert restored.params == {
        "fdr_cutoff": 0.2,
        "enable_modules": False,
        "max_missing_fraction": 0,
        "trans_log2": False,
        "metab_log2": False,
    }

    defaults = _build_job_params(
        atype=AnalysisType.GMA,
        counts=None, metadata=None, metabs=None, transcriptome=None, metabolome=None, group=None,
        compare_field=None, tested_levels=None, reference_level=None,
        padj_cutoff=None, log2fc_cutoff=None, min_total_count=None, min_replicates=None,
        same_fields=None, normalize=None, filter_low_expression=None, method=None,
        fdr_cutoff=None, enable_modules=None, vip_cutoff=None, pseudocount=None,
        max_missing_fraction=None, impute_method=None, log_transform=None,
        trans_log2=None, metab_log2=None, n_orthogonal_components=None,
    )
    agent_defaults = _build_params("GMA", {}, None).legacy_params()
    assert defaults == agent_defaults == {
        "fdr_cutoff": 0.05,
        "enable_modules": True,
        "max_missing_fraction": 0.5,
        "trans_log2": True,
        "metab_log2": True,
    }


def test_real_gma_example_preprocessing_uses_requested_parameters() -> None:
    platform_root = Path(__file__).resolve().parents[2]
    examples = platform_root / "frontend" / "public" / "examples" / "gma"
    adata = load_as_anndata(
        examples / "DEAT.csv",
        examples / "ym_metab.csv",
        group_table_path=examples / "group.csv",
    )
    adata = adata[:8, :20].copy()
    metabolomics = adata.obsm["metabolomics"].iloc[:, :10].copy()
    adata.obsm["metabolomics"] = metabolomics
    adata.uns["metabolite_names"] = metabolomics.columns.astype(str).tolist()

    processed = preprocess_adata(
        adata,
        missing_feature_threshold=0.4,
        trans_log2=False,
        metab_log2=False,
    )

    assert processed.uns["preprocess_summary"]["missing_feature_threshold"] == 0.4
    assert processed.uns["preprocess_summary"]["log_transform"] == {
        "transcriptome": False,
        "metabolomics": False,
    }

