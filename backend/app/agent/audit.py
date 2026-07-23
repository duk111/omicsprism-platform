from __future__ import annotations

from typing import Protocol

from .schemas import AgentEvent


class AgentEventStore(Protocol):
    """Append-only 事件接口，应用层不提供更新或删除方法。"""

    def append(self, event: AgentEvent) -> None:
        ...

    def list_for_run(self, *, run_id: str, user_id: str, limit: int = 100) -> list[AgentEvent]:
        ...
