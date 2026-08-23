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


class PostgresStateStore:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    def get(self, *, run_id: str, user_id: str) -> RunState:
        with self._connect() as conn:
            row = conn.execute(
                """
                select run_id, user_id, thread_id, active_profile, state, step_no,
                       focus, model_calls, tool_calls, status, version
                from agent_runs where run_id = %s and user_id = %s
                """,
                (run_id, user_id),
            ).fetchone()
        if row is None:
            raise StateNotFound(run_id)
        fields = (
            "run_id", "user_id", "thread_id", "active_profile", "state", "step_no",
            "focus", "model_calls", "tool_calls", "status", "version",
        )
        return RunState.model_validate(dict(zip(fields, row)))

    def save(self, state: RunState, *, expected_version: int) -> None:
        Jsonb = self._jsonb_type()
        next_version = expected_version + 1
        values = (
            state.thread_id,
            state.active_profile.value,
            state.state.value,
            state.step_no,
            Jsonb(state.focus.model_dump(mode="json")),
            state.model_calls,
            state.tool_calls,
            state.status.value,
            next_version,
            state.run_id,
            state.user_id,
            expected_version,
        )
        with self._connect() as conn:
            updated = conn.execute(
                """
                update agent_runs set
                    thread_id = %s,
                    active_profile = %s,
                    state = %s,
                    step_no = %s,
                    focus = %s,
                    model_calls = %s,
                    tool_calls = %s,
                    status = %s,
                    version = %s,
                    updated_at = now()
                where run_id = %s and user_id = %s and version = %s
                returning run_id
                """,
                values,
            ).fetchone()
            if updated is not None:
                return
            if expected_version != 0:
                raise StateConflict(f"expected version {expected_version}")
            try:
                conn.execute(
                    """
                    insert into agent_runs (
                        run_id, user_id, thread_id, active_profile, state, step_no,
                        focus, model_calls, tool_calls, status, version
                    ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        state.run_id,
                        state.user_id,
                        state.thread_id,
                        state.active_profile.value,
                        state.state.value,
                        state.step_no,
                        Jsonb(state.focus.model_dump(mode="json")),
                        state.model_calls,
                        state.tool_calls,
                        state.status.value,
                        next_version,
                    ),
                )
            except Exception as exc:
                raise StateConflict(f"expected version {expected_version}") from exc

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise RuntimeError("Install psycopg[binary]>=3.1.18 to use PostgreSQL storage") from exc
        return psycopg.connect(self.database_url)

    @staticmethod
    def _jsonb_type():
        try:
            from psycopg.types.json import Jsonb
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise RuntimeError("Install psycopg[binary]>=3.1.18 to use PostgreSQL storage") from exc
        return Jsonb
