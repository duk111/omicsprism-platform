from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .models import AnalysisType


CANONICAL_ROLE_ALIASES: dict[str, str] = {
    "metabs": "metabolome",
}
CANONICAL_INPUT_ROLES = frozenset({
    "counts", "metabolome", "transcriptome", "metadata", "group",
})


def canonical_input_role(role: str) -> str:
    """Normalize dataset roles before capability or execution decisions."""

    normalized = str(role).strip().casefold()
    return CANONICAL_ROLE_ALIASES.get(normalized, normalized)


def analysis_engine_role(role: str) -> str:
    """Map canonical roles to the existing analysis engine's file keys."""

    canonical = canonical_input_role(role)
    return next(
        (alias for alias, target in CANONICAL_ROLE_ALIASES.items() if target == canonical),
        canonical,
    )


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
    description: str = ""
    catalog_label: str = ""


class AnalysisCatalogItem(BaseModel):
    """Stable, model-facing description of one supported analysis module."""

    model_config = ConfigDict(extra="forbid")

    id: Literal["DEG", "DEM", "GMA"]
    label: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=800)
    required_inputs: list[str] = Field(default_factory=list, max_length=8)


class CapabilityItem(BaseModel):
    """Deterministic input-role capability for one registered analysis."""

    model_config = ConfigDict(extra="forbid")

    analysis_type: Literal["DEG", "DEM", "GMA"]
    supported: bool = True
    present_roles: list[str] = Field(default_factory=list, max_length=8)
    missing_roles: list[str] = Field(default_factory=list, max_length=8)
    next_step: Literal[
        "ready_for_parameter_resolution", "missing_input_role"
    ]


class CapabilityReport(BaseModel):
    """Bounded report returned by the capability query action."""

    model_config = ConfigDict(extra="forbid")

    items: list[CapabilityItem] = Field(default_factory=list, max_length=3)


_ANALYSIS_ORDER = (
    AnalysisType.DEG,
    AnalysisType.DEM,
    AnalysisType.GMA,
)


_DEFAULT_SPECS = {
    AnalysisType.DEG: AnalysisSpec(
        analysis_type=AnalysisType.DEG,
        display_label="DEG",
        catalog_label="差异基因分析",
        description="通过统计方法比较两组或多组样本的基因表达量，筛选出具有显著差异的基因",
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
        catalog_label="差异代谢物分析",
        description="通过统计方法比较两组或多组样本的代谢物丰度，筛选出具有显著差异的代谢物",
        input_rules=(InputRule("metabolome"), InputRule("metadata")),
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
    AnalysisType.GMA: AnalysisSpec(
        analysis_type=AnalysisType.GMA,
        display_label="GMA",
        catalog_label="基因-代谢调控网络分析",
        description="基于转录组、代谢组和分组信息推断基因-代谢物关联网络与关键基因",
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

    def canonical_role(self, role: str) -> str:
        return canonical_input_role(role)

    def engine_role(self, role: str) -> str:
        return analysis_engine_role(role)

    def accepted_input_roles(self) -> frozenset[str]:
        return CANONICAL_INPUT_ROLES | frozenset(CANONICAL_ROLE_ALIASES)

    def required_roles(self, analysis_type: AnalysisType | str) -> tuple[str, ...]:
        return tuple(
            self.canonical_role(rule.name)
            for rule in self.get(analysis_type).input_rules
            if rule.required
        )

    def analysis_catalog(self) -> list[AnalysisCatalogItem]:
        """Return the single source of truth for model-facing module knowledge."""

        return [
            AnalysisCatalogItem(
                id=analysis_type.name,
                label=(self.get(analysis_type).catalog_label or self.get(analysis_type).display_label),
                description=self.get(analysis_type).description,
                required_inputs=list(self.required_roles(analysis_type)),
            )
            for analysis_type in self.analysis_types()
        ]

    def capability_report(self, roles: Iterable[str]) -> CapabilityReport:
        present = {
            self.canonical_role(role)
            for role in roles
            if self.canonical_role(role) in CANONICAL_INPUT_ROLES
        }
        items: list[CapabilityItem] = []
        for analysis_type in self.analysis_types():
            missing = sorted(set(self.required_roles(analysis_type)) - present)
            items.append(CapabilityItem(
                analysis_type=analysis_type.name,
                present_roles=sorted(present & set(self.required_roles(analysis_type))),
                missing_roles=missing,
                next_step=(
                    "ready_for_parameter_resolution"
                    if not missing else "missing_input_role"
                ),
            ))
        return CapabilityReport(items=items)

    def get(self, analysis_type: AnalysisType | str) -> AnalysisSpec:
        return self._specs[AnalysisType(analysis_type)]

    def requested_params(self, analysis_type: AnalysisType | str, requested: dict[str, Any]) -> dict[str, Any]:
        """模型或用户参数只能进入对应 analysis spec 的白名单。"""
        allowed = {rule.name for rule in self.get(analysis_type).parameter_rules}
        return {name: value for name, value in requested.items() if name in allowed}

    def effective_params(self, analysis_type: AnalysisType | str, requested: dict[str, Any]) -> dict[str, Any]:
        spec = self.get(analysis_type)
        effective = self.requested_params(analysis_type, requested)
        for rule in spec.parameter_rules:
            if rule.name not in effective and rule.default is not None:
                effective[rule.name] = rule.default
        return effective
