from __future__ import annotations

from typing import Protocol

from .schemas import RunState


class RunCoordinator(Protocol):
    """单步运行时接口；本阶段不实现控制循环。"""

    def run_step(self, *, run_id: str, user_id: str, user_message: str) -> RunState:
        ...
