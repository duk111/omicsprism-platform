from __future__ import annotations

from typing import Any, Mapping, Protocol

from .schemas import ActiveProfile, RunState


class ContextBuilder(Protocol):
    """最小上下文构建接口；原始矩阵、完整 CSV 与完整日志不得进入。"""

    def build(
        self,
        *,
        state: RunState,
        active_profile: ActiveProfile,
        user_message: str,
    ) -> Mapping[str, Any]:
        ...
