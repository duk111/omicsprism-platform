from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from backend.app.job_execution import (
    _generate_deg_figure_data,
    _generate_dem_figure_data,
    _write_figure_data,
)


def _strict_json(path):
    return json.loads(
        path.read_text(encoding="utf-8"),
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)),
    )


def _trace_points(payload: dict) -> list[tuple[float, float, str]]:
    points = []
    for trace in payload["plotly_spec"]["data"]:
        points.extend(zip(trace["x"], trace["y"], trace["text"]))
    return points


def test_deg_volcano_data_is_strict_json_and_keeps_valid_zero_padj(tmp_path) -> None:
    pd.DataFrame(
        {
            "gene_id": ["up", "zero", "tiny", "missing", "bad_fc", "bad_padj"],
            "log2FoldChange": [2.0, -2.0, 0.5, 1.0, np.inf, 1.0],
            "padj": [0.01, 0.0, 1e-320, np.nan, 0.01, np.inf],
        }
    ).to_csv(tmp_path / "contrast.all.csv", index=False)

    _generate_deg_figure_data(tmp_path, {"padj_cutoff": 0.05, "log2fc_cutoff": 1})
    payload = _strict_json(tmp_path / "figure_data" / "volcano.json")

    points = _trace_points(payload)
    assert {point[2] for point in points} == {"up", "zero", "tiny"}
    assert all(np.isfinite(x) and np.isfinite(y) for x, y, _ in points)
    assert next(y for x, y, label in points if label == "zero") > 0
    assert len(payload["plotly_spec"]["layout"]["shapes"]) == 3
    assert payload["default_state"]["padj_threshold"] == 0.05


def test_dem_volcano_data_is_strict_json_and_zero_cutoff_omits_padj_line(tmp_path) -> None:
    pd.DataFrame(
        {
            "metabolite_id": ["normal", "zero", "missing", "bad_fc"],
            "log2FC": [2.0, 0.0, 1.0, np.inf],
            "FDR": [0.01, 0.0, np.nan, 0.01],
        }
    ).to_csv(tmp_path / "contrast.all.csv", index=False)

    _generate_dem_figure_data(tmp_path, {"padj_cutoff": 0.05, "log2fc_cutoff": 1})
    normal_payload = _strict_json(tmp_path / "figure_data" / "volcano.json")
    assert len(normal_payload["plotly_spec"]["layout"]["shapes"]) == 3
    assert normal_payload["plotly_spec"]["data"]

    _generate_dem_figure_data(tmp_path, {"padj_cutoff": 0, "log2fc_cutoff": 1})
    payload = _strict_json(tmp_path / "figure_data" / "volcano.json")

    points = _trace_points(payload)
    assert {point[2] for point in points} == {"normal", "zero"}
    assert all(np.isfinite(x) and np.isfinite(y) for x, y, _ in points)
    assert len(payload["plotly_spec"]["layout"]["shapes"]) == 2
    assert payload["default_state"]["padj_threshold"] == 0


def test_figure_writer_rejects_non_finite_values_and_converts_numpy_scalars(tmp_path) -> None:
    with pytest.raises(ValueError):
        _write_figure_data(tmp_path, "invalid", {"value": float("nan")})

    _write_figure_data(tmp_path, "valid", {"integer": np.int64(4), "value": np.float32(1.5)})
    payload = _strict_json(tmp_path / "figure_data" / "valid.json")
    assert payload == {"integer": 4, "value": pytest.approx(1.5)}
