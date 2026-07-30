from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from ..analysis_specs import AnalysisSpecRegistry
from .policy import ProfilePolicyGuard
from .schemas import ActiveProfile, AnalysisCapability, ModelContext, RunState, ToolResult


class ContextBuilder(Protocol):
    """最小上下文构建接口；原始矩阵、完整 CSV 与完整日志不得进入。"""

    def build(
        self,
        *,
        state: RunState,
        active_profile: ActiveProfile,
        user_message: str,
        available_input_roles: Sequence[str] = (),
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
        evidence: ToolResult | None = None,
    ) -> ModelContext:
        tools = sorted(ProfilePolicyGuard.allowed_tools(active_profile), key=lambda tool: tool.value)
        is_analysis = active_profile is ActiveProfile.ANALYSIS
        return ModelContext(
            user_message=user_message,
            active_profile=active_profile,
            state=state.state,
            in_scope_job_ids=list(state.focus.in_scope_job_ids),
            conversation_summary=None,
            available_input_roles=sorted(set(available_input_roles)) if is_analysis else [],
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
