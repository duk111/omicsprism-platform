from __future__ import annotations

from typing import Protocol

from .schemas import ActiveProfile, ToolName


class PolicyGuard(Protocol):
    """策略校验接口；本阶段不实现授权逻辑。"""

    def authorize(
        self,
        *,
        user_id: str,
        active_profile: ActiveProfile,
        tool: ToolName,
        resource_id: str | None = None,
        approval_id: str | None = None,
    ) -> None:
        ...


class ProfilePolicyViolation(PermissionError):
    """工具不属于当前 profile，或缺少调用所需的会话身份。"""


class ProfilePolicyGuard:
    ANALYSIS_TOOLS = frozenset({
        ToolName.INSPECT_UPLOADED_INPUTS,
        ToolName.GET_ANALYSIS_SPEC,
        ToolName.RUN_PREFLIGHT,
        ToolName.SUBMIT_APPROVED_PLAN,
        ToolName.GET_JOBS_STATUS,
    })
    INTERPRETATION_TOOLS = frozenset({ToolName.QUERY_RESULT_EVIDENCE, ToolName.GET_JOBS_STATUS})

    @classmethod
    def allowed_tools(cls, active_profile: ActiveProfile) -> frozenset[ToolName]:
        if active_profile is ActiveProfile.ANALYSIS:
            return cls.ANALYSIS_TOOLS
        return cls.INTERPRETATION_TOOLS

    def authorize(self, *, user_id: str, active_profile: ActiveProfile, tool: ToolName,
                  resource_id: str | None = None, approval_id: str | None = None) -> None:
        if not user_id:
            raise ProfilePolicyViolation("user identity is required")
        allowed = self.allowed_tools(active_profile)
        if tool not in allowed:
            raise ProfilePolicyViolation(f"{tool.value} is not allowed for {active_profile.value}")
