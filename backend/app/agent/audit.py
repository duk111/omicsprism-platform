from __future__ import annotations

from copy import deepcopy
from typing import Any, Protocol

from .schemas import AgentEvent


class AgentEventStore(Protocol):
    """Append-only 事件接口，应用层不提供更新或删除方法。"""

    def append(self, event: AgentEvent) -> None:
        ...

    def list_for_run(self, *, run_id: str, user_id: str, limit: int = 100) -> list[AgentEvent]:
        ...


class UnsafeTracePayload(ValueError):
    pass


class InMemoryAgentEventStore:
    """测试和单进程部署使用的 append-only trace 存储。"""

    def __init__(self) -> None:
        self._events: list[AgentEvent] = []
        self._event_ids: set[str] = set()

    def append(self, event: AgentEvent) -> None:
        _assert_safe_payload(event.payload)
        if event.event_id in self._event_ids:
            raise ValueError("agent event id already exists")
        self._event_ids.add(event.event_id)
        self._events.append(event.model_copy(deep=True))

    def list_for_run(self, *, run_id: str, user_id: str, limit: int = 100) -> list[AgentEvent]:
        bounded_limit = max(1, min(limit, 100))
        return [
            event.model_copy(deep=True)
            for event in self._events
            if event.run_id == run_id and event.user_id == user_id
        ][-bounded_limit:]


def _assert_safe_payload(payload: dict[str, Any]) -> None:
    forbidden = ("raw_", "database_url", "file_path", "password", "credential", "secret", "access_key", "token")
    for key, value in payload.items():
        normalized = str(key).lower()
        if any(term in normalized for term in forbidden):
            raise UnsafeTracePayload(f"trace payload key is not allowed: {key}")
        if isinstance(value, dict):
            _assert_safe_payload(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _assert_safe_payload(item)
