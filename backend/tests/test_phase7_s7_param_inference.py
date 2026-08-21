from __future__ import annotations

from backend.app.agent.runtime import _complete_contrast_params
from backend.app.agent.schemas import InputGroupLevels, InputInspectionSummary, InputValueCount
from backend.app.models import AnalysisType


def _summary(values: dict[str, int]) -> list[InputInspectionSummary]:
    return [InputInspectionSummary(
        field="metadata", columns=["sample_id", "treatment"], column_count=2,
        row_count=sum(values.values()), dtype=None,
        group_levels=[InputGroupLevels(
            column="treatment",
            values=[InputValueCount(value=key, count=count) for key, count in values.items()],
        )],
    )]


def test_complete_params_accepts_observed_normal_as_reference() -> None:
    params, question = _complete_contrast_params(
        AnalysisType.DIFFERENTIAL,
        {"compare_field": "treatment", "tested_levels": "NaCl", "reference_level": "Normal", "min_replicates": 2},
        _summary({"NaCl": 2, "Normal": 2}),
    )
    assert question is None
    assert params["reference_level"] == "Normal"


def test_complete_params_rejects_unknown_column_or_level() -> None:
    _, question = _complete_contrast_params(
        AnalysisType.DIFFERENTIAL,
        {"compare_field": "condition", "tested_levels": "NaCl", "reference_level": "Normal", "min_replicates": 2},
        _summary({"NaCl": 2, "Normal": 2}),
    )
    assert question and "不存在" in question


def test_complete_params_rejects_insufficient_replicates() -> None:
    _, question = _complete_contrast_params(
        AnalysisType.DIFFERENTIAL,
        {"compare_field": "treatment", "tested_levels": "NaCl", "reference_level": "Normal", "min_replicates": 2},
        _summary({"NaCl": 1, "Normal": 2}),
    )
    assert question and "样本数不足" in question
