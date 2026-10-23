from __future__ import annotations

import json
import io
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from fastapi import UploadFile

from backend.app.main import PREFLIGHT
from backend.app import job_execution
from backend.app.job_store import JobStorageService, LocalJsonJobRepository
from backend.app.models import AnalysisType, JobRecord, JobStatus


def _job(tmp_path: Path, analysis_type: AnalysisType, params: dict[str, object]) -> JobRecord:
    now = datetime.now(timezone.utc)
    return JobRecord(
        id=f"integration-{analysis_type.value}",
        project_name="scientific-integration",
        analysis_type=analysis_type,
        status=JobStatus.RUNNING,
        created_at=now,
        updated_at=now,
        params=params,
    )


def _store(tmp_path: Path) -> JobStorageService:
    return JobStorageService(LocalJsonJobRepository(tmp_path / "runs"))


def _write_differential_inputs(tmp_path: Path) -> tuple[Path, Path]:
    samples = [f"s{i}" for i in range(1, 7)]
    metadata = pd.DataFrame(
        {
            "sample_id": samples,
            "treatment": ["control"] * 3 + ["salt"] * 3,
        }
    )
    counts = pd.DataFrame(
        {
            sample: [
                100 + i * 2 if i < 3 else 420 + i * 3,
                200 + i,
                40 + (i % 3),
                80 + i * 2,
                25 + i,
                60 + (i * 2),
            ]
            for i, sample in enumerate(samples)
        },
        index=["gene_up", "gene_stable", "gene_low", "gene_mid", "gene_small", "gene_other"],
    )
    counts_path = tmp_path / "counts.csv"
    metadata_path = tmp_path / "metadata.csv"
    counts.to_csv(counts_path)
    metadata.to_csv(metadata_path, index=False)
    return counts_path, metadata_path


def _write_dem_inputs(tmp_path: Path) -> tuple[Path, Path]:
    samples = [f"s{i}" for i in range(1, 7)]
    metadata = pd.DataFrame(
        {
            "sample_id": samples,
            "treatment": ["control"] * 3 + ["salt"] * 3,
        }
    )
    values = {
        sample: [
            10.0 + i if i < 3 else 80.0 + i,
            25.0 + i,
            np.nan if i == 1 else 5.0 + i,
            40.0 + (i % 2),
            100.0 + i * 2,
            2.0 + i,
        ]
        for i, sample in enumerate(samples)
    }
    metabs = pd.DataFrame(values, index=["metab_up", "metab_stable", "metab_missing", "metab_flat", "metab_high", "metab_low"])
    metabs_path = tmp_path / "metabs.csv"
    metadata_path = tmp_path / "metadata.csv"
    metabs.to_csv(metabs_path)
    metadata.to_csv(metadata_path, index=False)
    return metabs_path, metadata_path


def _write_gma_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    samples = [f"s{i}" for i in range(1, 9)]
    group = pd.DataFrame(
        {
            "sample_id": samples,
            "group1": ["control"] * 4 + ["salt"] * 4,
            "group2": ["batch1", "batch1", "batch2", "batch2"] * 2,
        }
    )
    transcriptome = pd.DataFrame(
        {
            sample: [
                2.0 + i * 0.2,
                4.0 + (i % 3) * 0.4,
                6.0 + i * 0.1,
                8.0 + (i % 2),
                10.0 + i * 0.3,
                12.0 + (i % 4) * 0.2,
                14.0 + i * 0.15,
                16.0 + (i % 3) * 0.3,
                18.0 + i * 0.05,
                20.0 + (i % 2) * 0.5,
                22.0 + i * 0.12,
                24.0 + (i % 4) * 0.2,
            ]
            for i, sample in enumerate(samples)
        },
        index=[f"gene_{i}" for i in range(12)],
    )
    metabolome = pd.DataFrame(
        {
            sample: [
                5.0 + i * 0.1,
                7.0 + (i % 2) * 0.2,
                9.0 + i * 0.15,
                11.0 + (i % 3) * 0.3,
                13.0 + i * 0.08,
                15.0 + (i % 2) * 0.4,
            ]
            for i, sample in enumerate(samples)
        },
        index=[f"metab_{i}" for i in range(6)],
    )
    transcriptome_path = tmp_path / "transcriptome.csv"
    metabolome_path = tmp_path / "metabolome.csv"
    group_path = tmp_path / "group.csv"
    transcriptome.to_csv(transcriptome_path)
    metabolome.to_csv(metabolome_path)
    group.to_csv(group_path, index=False)
    return transcriptome_path, metabolome_path, group_path


def _assert_strict_json_files(output_dir: Path) -> None:
    for path in output_dir.rglob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        assert payload is not None


def _preflight(analysis_type: AnalysisType, params: dict[str, object], paths: dict[str, Path]) -> None:
    uploads = {
        field: UploadFile(filename=f"{field}.csv", file=io.BytesIO(path.read_bytes()))
        for field, path in paths.items()
    }
    result = PREFLIGHT.preflight(analysis_type, params=params, files=uploads)
    assert result.can_submit, result.model_dump()


def test_worker_executes_real_deg_pipeline_and_writes_results(tmp_path: Path) -> None:
    counts, metadata = _write_differential_inputs(tmp_path)
    output = tmp_path / "deg-output"
    params = {"compare_field": "treatment", "tested_levels": "salt", "reference_level": "control", "min_total_count": 0, "min_replicates": 2, "padj_cutoff": 1.0, "log2fc_cutoff": 0}
    _preflight(AnalysisType.DEG, params, {"counts": counts, "metadata": metadata})
    job = _job(tmp_path, AnalysisType.DEG, params)

    job_execution._run_differential_job(job, _store(tmp_path), tmp_path, output, {"counts": counts, "metadata": metadata}, lambda *_args: None)

    tables = list(output.glob("*.all.csv"))
    assert tables
    result = pd.read_csv(tables[0])
    assert {"gene_id", "log2FoldChange", "pvalue", "padj", "comparison"}.issubset(result.columns)
    assert (output / "plots" / "volcano").exists()
    _assert_strict_json_files(output)


def test_worker_executes_real_dem_pipeline_with_missing_values(tmp_path: Path) -> None:
    metabs, metadata = _write_dem_inputs(tmp_path)
    output = tmp_path / "dem-output"
    params = {"compare_field": "treatment", "tested_levels": "salt", "reference_level": "control", "min_replicates": 2, "vip_cutoff": 0, "padj_cutoff": 1.0, "log2fc_cutoff": 0, "max_missing_fraction": 0.5, "n_orthogonal_components": 0}
    _preflight(AnalysisType.DEM, params, {"metabs": metabs, "metadata": metadata})
    job = _job(tmp_path, AnalysisType.DEM, params)

    job_execution._run_dem_job(job, _store(tmp_path), tmp_path, output, {"metabs": metabs, "metadata": metadata}, lambda *_args: None)

    tables = list(output.glob("*.all.csv"))
    assert tables
    result = pd.read_csv(tables[0])
    assert {"metabolite_id", "vip", "log2FoldChange", "pvalue", "padj_bh", "dem_status"}.issubset(result.columns)
    assert np.isfinite(result.select_dtypes(include=[np.number]).to_numpy()).all()
    _assert_strict_json_files(output)


def test_worker_executes_real_gma_pipeline_and_writes_network_tables(tmp_path: Path) -> None:
    transcriptome, metabolome, group = _write_gma_inputs(tmp_path)
    output = tmp_path / "gma-output"
    params = {"fdr_cutoff": 0.5, "enable_modules": False, "max_missing_fraction": 0.5, "trans_log2": False, "metab_log2": False}
    _preflight(AnalysisType.GMA, params, {"transcriptome": transcriptome, "metabolome": metabolome, "group": group})
    job = _job(tmp_path, AnalysisType.GMA, params)

    job_execution._run_correlation_job(job, _store(tmp_path), tmp_path, output, {"transcriptome": transcriptome, "metabolome": metabolome, "group": group}, lambda *_args: None)

    network = output / "T02_High_Confidence_Network.csv"
    assert network.exists()
    result = pd.read_csv(network)
    assert {"Gene", "Metabolite", "EdgeWeight"}.issubset(result.columns)
    assert np.isfinite(result.select_dtypes(include=[np.number]).to_numpy()).all()
    assert any(output.rglob("*.png"))
    _assert_strict_json_files(output)
