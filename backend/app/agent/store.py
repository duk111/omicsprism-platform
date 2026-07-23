from __future__ import annotations

from typing import Protocol

from .schemas import RunState


class StateStore(Protocol):
    """RunState 持久化接口，所有读取都绑定 run_id 与 user_id。"""

    def get(self, *, run_id: str, user_id: str) -> RunState:
        ...

    def save(self, state: RunState, *, expected_version: int) -> None:
        ...
