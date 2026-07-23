from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .models import AnalysisType


@dataclass(frozen=True)
class InputRule:
    name: str
    required: bool = True


@dataclass(frozen=True)
class ParameterRule:
    name: str
    required: bool = False
    default: Any = None


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
            ParameterRule("padj_cutoff", default=0.05),
            ParameterRule("log2fc_cutoff", default=1.0),
            ParameterRule("min_total_count", default=10),
            ParameterRule("min_replicates", default=2),
            ParameterRule("normalize", default=True),
            ParameterRule("filter_low_expression", default=True),
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
            ParameterRule("padj_cutoff", default=0.05),
            ParameterRule("log2fc_cutoff", default=1.0),
            ParameterRule("vip_cutoff", default=1.0),
            ParameterRule("pseudocount", default=1e-9),
            ParameterRule("max_missing_fraction", default=0.5),
            ParameterRule("impute_method", default="half-min"),
            ParameterRule("normalize", default=True),
            ParameterRule("log_transform", default=True),
            ParameterRule("min_replicates", default=2),
            ParameterRule("n_orthogonal_components", default=1),
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
        parameter_rules=(
            ParameterRule("fdr_cutoff", default=0.05),
            ParameterRule("enable_modules", default=True),
            ParameterRule("trans_log2", default=True),
            ParameterRule("metab_log2", default=True),
            ParameterRule("max_missing_fraction", default=0.5),
        ),
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

    def effective_params(self, analysis_type: AnalysisType | str, requested: dict[str, Any]) -> dict[str, Any]:
        spec = self.get(analysis_type)
        effective = dict(requested)
        for rule in spec.parameter_rules:
            if rule.name not in effective and rule.default is not None:
                effective[rule.name] = rule.default
        return effective
