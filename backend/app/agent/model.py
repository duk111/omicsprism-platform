from __future__ import annotations

from typing import Any, Mapping, Protocol

from .schemas import AgentDecision


class ModelAdapter(Protocol):
    """模型适配接口；不校验、不授权，也不持有业务句柄。"""

    def decide(self, context: Mapping[str, Any]) -> AgentDecision:
        ...
