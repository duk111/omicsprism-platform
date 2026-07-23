from __future__ import annotations

from typing import Protocol

from .schemas import RouteDecision, RunState


class Router(Protocol):
    """Phase 2 才实现路由逻辑；Phase 0 只固定接口。"""

    def route(self, user_message: str, state: RunState) -> RouteDecision:
        ...
