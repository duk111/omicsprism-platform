from __future__ import annotations

from dataclasses import dataclass

from .models import AnalysisType


@dataclass(frozen=True)
class InputRule:
    name: str
    required: bool = True


@dataclass(frozen=True)
class ParameterRule:
    name: str
    required: bool = False


@dataclass(frozen=True)
class AnalysisSpec:
    analysis_type: AnalysisType
    display_label: str
    input_rules: tuple[InputRule, ...]
    parameter_rules: tuple[ParameterRule, ...]


_ANALYSIS_ORDER = (
    AnalysisType.DIFFERENTIAL,
    AnalysisType.DEM,
    AnalysisType.CORRELATION,
)


_DEFAULT_SPECS = {
    AnalysisType.DIFFERENTIAL: AnalysisSpec(
        analysis_type=AnalysisType.DIFFERENTIAL,
        display_label="DEG",
        input_rules=(InputRule("counts"), InputRule("metadata")),
        parameter_rules=(
            ParameterRule("compare_field", required=True),
            ParameterRule("tested_levels", required=True),
            ParameterRule("reference_level", required=True),
            ParameterRule("same_fields"),
        ),
    ),
    AnalysisType.DEM: AnalysisSpec(
        analysis_type=AnalysisType.DEM,
        display_label="DEM",
        input_rules=(InputRule("metabs"), InputRule("metadata")),
        parameter_rules=(
            ParameterRule("compare_field", required=True),
            ParameterRule("tested_levels", required=True),
            ParameterRule("reference_level", required=True),
            ParameterRule("same_fields"),
        ),
    ),
    AnalysisType.CORRELATION: AnalysisSpec(
        analysis_type=AnalysisType.CORRELATION,
        display_label="GMA",
        input_rules=(
            InputRule("transcriptome"),
            InputRule("metabolome"),
            InputRule("group"),
        ),
        parameter_rules=(),
    ),
}


class AnalysisSpecRegistry:
    """DEG/DEM/GMA 规则容器；内部只使用既有 AnalysisType。"""

    def __init__(self, specs: dict[AnalysisType, AnalysisSpec] | None = None) -> None:
        self._specs = dict(specs or _DEFAULT_SPECS)

    def analysis_types(self) -> tuple[AnalysisType, ...]:
        return tuple(item for item in _ANALYSIS_ORDER if item in self._specs)

    def get(self, analysis_type: AnalysisType | str) -> AnalysisSpec:
        return self._specs[AnalysisType(analysis_type)]
