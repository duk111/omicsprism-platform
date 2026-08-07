from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from ..analysis_specs import AnalysisSpecRegistry
from .policy import ProfilePolicyGuard
from .schemas import (
    ActiveProfile,
    AgentState,
    AnalysisCapability,
    InputGroupLevels,
    InputInspectionSummary,
    InputValueCount,
    ModelContext,
    RunState,
    ToolResult,
)


class ContextBuilder(Protocol):
    """最小上下文构建接口；原始矩阵、完整 CSV 与完整日志不得进入。"""

    def build(
        self,
        *,
        state: RunState,
        active_profile: ActiveProfile,
        user_message: str,
        available_input_roles: Sequence[str] = (),
        input_summaries: Sequence[InputInspectionSummary] = (),
        evidence: ToolResult | None = None,
    ) -> ModelContext:
        ...


class MinimalContextBuilder:
    def __init__(self, analysis_specs: AnalysisSpecRegistry | None = None) -> None:
        self.analysis_specs = analysis_specs or AnalysisSpecRegistry()

    def build(
        self,
        *,
        state: RunState,
        active_profile: ActiveProfile,
        user_message: str,
        available_input_roles: Sequence[str] = (),
        input_summaries: Sequence[InputInspectionSummary] = (),
        evidence: ToolResult | None = None,
    ) -> ModelContext:
        tools = (
            []
            if state.state is AgentState.ADVISE
            else sorted(ProfilePolicyGuard.allowed_tools(active_profile), key=lambda tool: tool.value)
        )
        is_analysis = active_profile is ActiveProfile.ANALYSIS
        return ModelContext(
            user_message=user_message,
            active_profile=active_profile,
            state=state.state,
            in_scope_job_ids=list(state.focus.in_scope_job_ids),
            conversation_summary=None,
            available_input_roles=sorted(set(available_input_roles)) if is_analysis else [],
            input_summaries=list(input_summaries) if is_analysis else [],
            analysis_capabilities=build_analysis_capabilities(self.analysis_specs) if is_analysis else [],
            available_tools=tools,
            evidence=evidence,
        )


def build_analysis_capabilities(
    registry: AnalysisSpecRegistry | None = None,
) -> list[AnalysisCapability]:
    specs = registry or AnalysisSpecRegistry()
    return [
        AnalysisCapability(
            analysis_type=analysis_type,
            display_label=spec.display_label,
            required_inputs=[rule.name for rule in spec.input_rules if rule.required],
        )
        for analysis_type in specs.analysis_types()
        for spec in [specs.get(analysis_type)]
    ]


def build_input_summaries(rows: Sequence[Mapping[str, object]]) -> list[InputInspectionSummary]:
    """只保留列名、行数和分组计数，不把原始数据行放入模型上下文。"""

    summaries: list[InputInspectionSummary] = []
    for row in rows[:6]:
        raw_groups = row.get("group_replicates")
        groups: list[InputGroupLevels] = []
        if isinstance(raw_groups, Mapping):
            for column, raw_counts in list(raw_groups.items())[:20]:
                if not isinstance(raw_counts, Mapping):
                    continue
                values = [
                    InputValueCount(value=str(value), count=max(0, int(count)))
                    for value, count in list(raw_counts.items())[:12]
                ]
                groups.append(InputGroupLevels(column=str(column), values=values))
        raw_columns = row.get("columns")
        columns = [str(item) for item in list(raw_columns)[:40]] if isinstance(raw_columns, list) else []
        dtype = row.get("dtype")
        summaries.append(InputInspectionSummary(
            field=str(row.get("field") or "unknown"),
            columns=columns,
            row_count=max(0, int(row.get("row_count") or 0)),
            dtype=str(dtype) if dtype is not None else None,
            group_levels=groups,
        ))
    return summaries
