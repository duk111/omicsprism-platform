from __future__ import annotations

from typing import Protocol

from .policy import ProfilePolicyGuard
from .schemas import ActiveProfile, ModelContext, RunState


class ContextBuilder(Protocol):
    """最小上下文构建接口；原始矩阵、完整 CSV 与完整日志不得进入。"""

    def build(
        self,
        *,
        state: RunState,
        active_profile: ActiveProfile,
        user_message: str,
    ) -> ModelContext:
        ...


class MinimalContextBuilder:
    def build(self, *, state: RunState, active_profile: ActiveProfile, user_message: str) -> ModelContext:
        tools = sorted(ProfilePolicyGuard.allowed_tools(active_profile), key=lambda tool: tool.value)
        return ModelContext(
            user_message=user_message,
            active_profile=active_profile,
            state=state.state,
            in_scope_job_ids=list(state.focus.in_scope_job_ids),
            conversation_summary=None,
            available_tools=tools,
        )
