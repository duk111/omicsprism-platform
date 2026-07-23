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
