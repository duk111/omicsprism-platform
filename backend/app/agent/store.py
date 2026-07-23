from __future__ import annotations

from copy import deepcopy
from typing import Any, Protocol

from .schemas import RunState


class StateStore(Protocol):
    """RunState 持久化接口，所有读取都绑定 run_id 与 user_id。"""

    def get(self, *, run_id: str, user_id: str) -> RunState:
        ...

    def save(self, state: RunState, *, expected_version: int) -> None:
        ...


class StateConflict(RuntimeError):
    pass


class StateNotFound(LookupError):
    pass


class InMemoryStateStore:
    def __init__(self, shared: dict[str, Any] | None = None) -> None:
        self._shared = shared if shared is not None else {}

    def get(self, *, run_id: str, user_id: str) -> RunState:
        payload = self._shared.get(run_id)
        if payload is None or payload.get("user_id") != user_id:
            raise StateNotFound(run_id)
        return RunState.model_validate(deepcopy(payload))

    def save(self, state: RunState, *, expected_version: int) -> None:
        current = self._shared.get(state.run_id)
        if current is not None and current.get("user_id") != state.user_id:
            raise StateConflict("run belongs to another user")
        actual = int(current.get("version", 0)) if current is not None else 0
        if actual != expected_version:
            raise StateConflict(f"expected version {expected_version}, found {actual}")
        next_state = state.model_copy(deep=True)
        next_state.version = expected_version + 1
        self._shared[state.run_id] = next_state.model_dump(mode="json")
