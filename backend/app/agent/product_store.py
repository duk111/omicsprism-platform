from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta
import threading
from typing import Any, Iterator, Protocol

from .schemas import (
    AgentEvent,
    AgentInputBundleRecord,
    AgentInputFileRecord,
    AgentMessageBlock,
    AgentMessageRecord,
    AgentMessageRole,
    AgentThreadRecord,
    AgentTurnRecord,
    AgentTurnStatus,
    RunState,
)
from .store import StateConflict


class AgentResourceNotFound(LookupError):
    pass


class IdempotencyConflict(RuntimeError):
    pass


class ActiveTurnConflict(RuntimeError):
    pass


class TurnLeaseMismatch(RuntimeError):
    pass


class AgentProductStore(Protocol):
    def save_thread(self, thread: AgentThreadRecord) -> None:
        ...

    def get_thread(self, *, thread_id: str, user_id: str) -> AgentThreadRecord:
        ...

    def append_message(self, message: AgentMessageRecord) -> None:
        ...

    def list_messages(self, *, thread_id: str, user_id: str, limit: int = 100) -> list[AgentMessageRecord]:
        ...

    def create_turn(self, turn: AgentTurnRecord) -> AgentTurnRecord:
        ...

    def get_turn(self, *, turn_id: str, user_id: str) -> AgentTurnRecord:
        ...

    def list_turns(self, *, thread_id: str, user_id: str, limit: int = 100) -> list[AgentTurnRecord]:
        ...

    def claim_next_turn(self, *, worker_id: str, now: datetime, lease_seconds: int) -> AgentTurnRecord | None:
        ...

    def finish_turn(self, *, turn_id: str, user_id: str, worker_id: str,
                    status: AgentTurnStatus, now: datetime, error_code: str | None = None) -> AgentTurnRecord:
        ...

    def worker_slot(self) -> Iterator[bool]:
        ...

    def save_input_bundle(self, bundle: AgentInputBundleRecord) -> None:
        ...

    def get_input_bundle(self, *, bundle_id: str, user_id: str) -> AgentInputBundleRecord:
        ...

    def append_input_file(self, item: AgentInputFileRecord) -> None:
        ...

    def list_input_files(self, *, bundle_id: str, user_id: str) -> list[AgentInputFileRecord]:
        ...


class InMemoryAgentProductStore:
    """普通 CI 使用的精确 repository 契约，不生成业务假数据。"""

    _worker_lock = threading.Lock()

    def __init__(self, shared: dict[str, Any] | None = None) -> None:
        self._shared = shared if shared is not None else {}
        self._threads = self._shared.setdefault("threads", {})
        self._messages = self._shared.setdefault("messages", {})
        self._turns = self._shared.setdefault("turns", {})
        self._turn_keys = self._shared.setdefault("turn_keys", {})
        self._bundles = self._shared.setdefault("bundles", {})
        self._files = self._shared.setdefault("files", {})

    def save_thread(self, thread: AgentThreadRecord) -> None:
        current = self._threads.get(thread.thread_id)
        if current is not None and current["user_id"] != thread.user_id:
            raise AgentResourceNotFound(thread.thread_id)
        self._threads[thread.thread_id] = thread.model_dump(mode="json")

    def get_thread(self, *, thread_id: str, user_id: str) -> AgentThreadRecord:
        payload = self._threads.get(thread_id)
        if payload is None or payload["user_id"] != user_id:
            raise AgentResourceNotFound(thread_id)
        return AgentThreadRecord.model_validate(deepcopy(payload))

    def append_message(self, message: AgentMessageRecord) -> None:
        self.get_thread(thread_id=message.thread_id, user_id=message.user_id)
        if message.message_id in self._messages:
            raise ValueError("agent message id already exists")
        self._messages[message.message_id] = message.model_dump(mode="json")

    def list_messages(self, *, thread_id: str, user_id: str, limit: int = 100) -> list[AgentMessageRecord]:
        self.get_thread(thread_id=thread_id, user_id=user_id)
        bounded = max(1, min(limit, 100))
        records = [
            AgentMessageRecord.model_validate(deepcopy(payload))
            for payload in self._messages.values()
            if payload["thread_id"] == thread_id and payload["user_id"] == user_id
        ]
        records.sort(key=lambda item: (item.created_at, item.message_id))
        return records[-bounded:]

    def create_turn(self, turn: AgentTurnRecord) -> AgentTurnRecord:
        self.get_thread(thread_id=turn.thread_id, user_id=turn.user_id)
        key = (turn.user_id, turn.idempotency_key)
        existing_id = self._turn_keys.get(key)
        if existing_id is not None:
            existing = AgentTurnRecord.model_validate(deepcopy(self._turns[existing_id]))
            if (
                existing.request_hash != turn.request_hash
                or existing.thread_id != turn.thread_id
                or existing.run_id != turn.run_id
            ):
                raise IdempotencyConflict(turn.idempotency_key)
            return existing
        if any(
            payload["thread_id"] == turn.thread_id
            and payload["user_id"] == turn.user_id
            and payload["status"] in {AgentTurnStatus.QUEUED.value, AgentTurnStatus.RUNNING.value}
            for payload in self._turns.values()
        ):
            raise ActiveTurnConflict(turn.thread_id)
        if turn.turn_id in self._turns:
            raise ValueError("agent turn id already exists")
        self._turns[turn.turn_id] = turn.model_dump(mode="json")
        self._turn_keys[key] = turn.turn_id
        return turn.model_copy(deep=True)

    def get_turn(self, *, turn_id: str, user_id: str) -> AgentTurnRecord:
        payload = self._turns.get(turn_id)
        if payload is None or payload["user_id"] != user_id:
            raise AgentResourceNotFound(turn_id)
        return AgentTurnRecord.model_validate(deepcopy(payload))

    def list_turns(self, *, thread_id: str, user_id: str, limit: int = 100) -> list[AgentTurnRecord]:
        self.get_thread(thread_id=thread_id, user_id=user_id)
        bounded = max(1, min(limit, 100))
        records = [
            AgentTurnRecord.model_validate(deepcopy(payload))
            for payload in self._turns.values()
            if payload["thread_id"] == thread_id and payload["user_id"] == user_id
        ]
        records.sort(key=lambda item: (item.created_at, item.turn_id))
        return records[-bounded:]

    def claim_next_turn(self, *, worker_id: str, now: datetime, lease_seconds: int) -> AgentTurnRecord | None:
        if not worker_id or lease_seconds < 1:
            raise ValueError("worker_id and a positive lease are required")
        eligible = [
            AgentTurnRecord.model_validate(deepcopy(payload))
            for payload in self._turns.values()
            if payload["status"] == AgentTurnStatus.QUEUED.value
            or (
                payload["status"] == AgentTurnStatus.RUNNING.value
                and payload.get("lease_expires_at") is not None
                and datetime.fromisoformat(payload["lease_expires_at"]) <= now
            )
        ]
        eligible.sort(key=lambda item: (item.created_at, item.turn_id))
        if not eligible:
            return None
        turn = eligible[0]
        turn.status = AgentTurnStatus.RUNNING
        turn.attempt += 1
        turn.lease_owner = worker_id
        turn.lease_expires_at = now + timedelta(seconds=lease_seconds)
        turn.started_at = turn.started_at or now
        turn.updated_at = now
        self._turns[turn.turn_id] = turn.model_dump(mode="json")
        return turn.model_copy(deep=True)

    def finish_turn(self, *, turn_id: str, user_id: str, worker_id: str,
                    status: AgentTurnStatus, now: datetime, error_code: str | None = None) -> AgentTurnRecord:
        if status not in {AgentTurnStatus.COMPLETED, AgentTurnStatus.FAILED}:
            raise ValueError("turn may only finish as completed or failed")
        turn = self.get_turn(turn_id=turn_id, user_id=user_id)
        if turn.status is not AgentTurnStatus.RUNNING or turn.lease_owner != worker_id:
            raise TurnLeaseMismatch(turn_id)
        turn.status = status
        turn.error_code = error_code
        turn.lease_owner = None
        turn.lease_expires_at = None
        turn.completed_at = now
        turn.updated_at = now
        self._turns[turn.turn_id] = turn.model_dump(mode="json")
        return turn.model_copy(deep=True)

    @contextmanager
    def worker_slot(self) -> Iterator[bool]:
        acquired = self._worker_lock.acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                self._worker_lock.release()

    def save_input_bundle(self, bundle: AgentInputBundleRecord) -> None:
        self.get_thread(thread_id=bundle.thread_id, user_id=bundle.user_id)
        current = self._bundles.get(bundle.bundle_id)
        if current is not None and current["user_id"] != bundle.user_id:
            raise AgentResourceNotFound(bundle.bundle_id)
        self._bundles[bundle.bundle_id] = bundle.model_dump(mode="json")

    def get_input_bundle(self, *, bundle_id: str, user_id: str) -> AgentInputBundleRecord:
        payload = self._bundles.get(bundle_id)
        if payload is None or payload["user_id"] != user_id:
            raise AgentResourceNotFound(bundle_id)
        return AgentInputBundleRecord.model_validate(deepcopy(payload))

    def append_input_file(self, item: AgentInputFileRecord) -> None:
        self.get_input_bundle(bundle_id=item.bundle_id, user_id=item.user_id)
        if item.file_id in self._files:
            raise ValueError("agent input file id already exists")
        self._files[item.file_id] = item.model_dump(mode="json")

    def list_input_files(self, *, bundle_id: str, user_id: str) -> list[AgentInputFileRecord]:
        self.get_input_bundle(bundle_id=bundle_id, user_id=user_id)
        records = [
            AgentInputFileRecord.model_validate(deepcopy(payload))
            for payload in self._files.values()
            if payload["bundle_id"] == bundle_id and payload["user_id"] == user_id
        ]
        records.sort(key=lambda item: (item.created_at, item.file_id))
        return records


class PostgresAgentProductStore:
    """产品对话表的 PostgreSQL repository；所有资源读取都绑定用户。"""

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    def save_thread(self, thread: AgentThreadRecord) -> None:
        with self._connect() as conn:
            row = conn.execute(
                """
                insert into agent_threads (
                    thread_id, user_id, title, current_run_id, status, version, created_at, updated_at
                ) values (%s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (thread_id) do update set
                    title = excluded.title,
                    current_run_id = excluded.current_run_id,
                    status = excluded.status,
                    version = excluded.version,
                    updated_at = excluded.updated_at
                where agent_threads.user_id = excluded.user_id
                returning thread_id
                """,
                (
                    thread.thread_id,
                    thread.user_id,
                    thread.title,
                    thread.current_run_id,
                    thread.status.value,
                    thread.version,
                    thread.created_at,
                    thread.updated_at,
                ),
            ).fetchone()
        if row is None:
            raise AgentResourceNotFound(thread.thread_id)

    def get_thread(self, *, thread_id: str, user_id: str) -> AgentThreadRecord:
        with self._connect() as conn:
            row = conn.execute(
                """
                select thread_id, user_id, title, current_run_id, status, version, created_at, updated_at
                from agent_threads where thread_id = %s and user_id = %s
                """,
                (thread_id, user_id),
            ).fetchone()
        if row is None:
            raise AgentResourceNotFound(thread_id)
        return _thread_from_row(row)

    def append_message(self, message: AgentMessageRecord) -> None:
        self.get_thread(thread_id=message.thread_id, user_id=message.user_id)
        Jsonb = _jsonb_type()
        with self._connect() as conn:
            conn.execute(
                """
                insert into agent_messages (
                    message_id, thread_id, run_id, user_id, role, blocks, created_at
                ) values (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    message.message_id,
                    message.thread_id,
                    message.run_id,
                    message.user_id,
                    message.role.value,
                    Jsonb([block.model_dump(mode="json") for block in message.blocks]),
                    message.created_at,
                ),
            )

    def list_messages(self, *, thread_id: str, user_id: str, limit: int = 100) -> list[AgentMessageRecord]:
        self.get_thread(thread_id=thread_id, user_id=user_id)
        bounded = max(1, min(limit, 100))
        with self._connect() as conn:
            rows = conn.execute(
                """
                select message_id, thread_id, run_id, user_id, role, blocks, created_at
                from agent_messages
                where thread_id = %s and user_id = %s
                order by created_at desc, message_id desc limit %s
                """,
                (thread_id, user_id, bounded),
            ).fetchall()
        return [_message_from_row(row) for row in reversed(rows)]

    def create_turn(self, turn: AgentTurnRecord) -> AgentTurnRecord:
        self.get_thread(thread_id=turn.thread_id, user_id=turn.user_id)
        try:
            with self._connect() as conn:
                row = conn.execute(
                    """
                    insert into agent_turns (
                        turn_id, thread_id, run_id, user_id, idempotency_key, request_hash,
                        status, attempt, lease_owner, lease_expires_at, error_code,
                        created_at, updated_at, started_at, completed_at
                    ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    on conflict (user_id, idempotency_key) do nothing
                    returning turn_id
                    """,
                    _turn_values(turn),
                ).fetchone()
                if row is not None:
                    return turn.model_copy(deep=True)
                existing_row = conn.execute(
                    """
                    select turn_id, thread_id, run_id, user_id, idempotency_key, request_hash,
                           status, attempt, lease_owner, lease_expires_at, error_code,
                           created_at, updated_at, started_at, completed_at
                    from agent_turns where user_id = %s and idempotency_key = %s
                    """,
                    (turn.user_id, turn.idempotency_key),
                ).fetchone()
        except Exception as exc:
            if _constraint_name(exc) == "agent_turns_one_active_per_thread_idx":
                raise ActiveTurnConflict(turn.thread_id) from exc
            raise
        if existing_row is None:
            raise RuntimeError("idempotent turn insert returned no record")
        existing = _turn_from_row(existing_row)
        if (
            existing.request_hash != turn.request_hash
            or existing.thread_id != turn.thread_id
            or existing.run_id != turn.run_id
        ):
            raise IdempotencyConflict(turn.idempotency_key)
        return existing

    def get_turn(self, *, turn_id: str, user_id: str) -> AgentTurnRecord:
        with self._connect() as conn:
            row = conn.execute(
                """
                select turn_id, thread_id, run_id, user_id, idempotency_key, request_hash,
                       status, attempt, lease_owner, lease_expires_at, error_code,
                       created_at, updated_at, started_at, completed_at
                from agent_turns where turn_id = %s and user_id = %s
                """,
                (turn_id, user_id),
            ).fetchone()
        if row is None:
            raise AgentResourceNotFound(turn_id)
        return _turn_from_row(row)

    def list_turns(self, *, thread_id: str, user_id: str, limit: int = 100) -> list[AgentTurnRecord]:
        self.get_thread(thread_id=thread_id, user_id=user_id)
        bounded = max(1, min(limit, 100))
        with self._connect() as conn:
            rows = conn.execute(
                """
                select turn_id, thread_id, run_id, user_id, idempotency_key, request_hash,
                       status, attempt, lease_owner, lease_expires_at, error_code,
                       created_at, updated_at, started_at, completed_at
                from agent_turns
                where thread_id = %s and user_id = %s
                order by created_at desc, turn_id desc limit %s
                """,
                (thread_id, user_id, bounded),
            ).fetchall()
        return [_turn_from_row(row) for row in reversed(rows)]

    def claim_next_turn(self, *, worker_id: str, now: datetime, lease_seconds: int) -> AgentTurnRecord | None:
        if not worker_id or lease_seconds < 1:
            raise ValueError("worker_id and a positive lease are required")
        lease_expires_at = now + timedelta(seconds=lease_seconds)
        with self._connect() as conn:
            row = conn.execute(
                """
                with candidate as (
                    select turn_id
                    from agent_turns
                    where status = 'queued'
                       or (status = 'running' and lease_expires_at <= %s)
                    order by created_at, turn_id
                    for update skip locked
                    limit 1
                )
                update agent_turns as turn set
                    status = 'running',
                    attempt = turn.attempt + 1,
                    lease_owner = %s,
                    lease_expires_at = %s,
                    started_at = coalesce(turn.started_at, %s),
                    updated_at = %s
                from candidate
                where turn.turn_id = candidate.turn_id
                returning turn.turn_id, turn.thread_id, turn.run_id, turn.user_id,
                          turn.idempotency_key, turn.request_hash, turn.status, turn.attempt,
                          turn.lease_owner, turn.lease_expires_at, turn.error_code,
                          turn.created_at, turn.updated_at, turn.started_at, turn.completed_at
                """,
                (now, worker_id, lease_expires_at, now, now),
            ).fetchone()
        return _turn_from_row(row) if row is not None else None

    def finish_turn(self, *, turn_id: str, user_id: str, worker_id: str,
                    status: AgentTurnStatus, now: datetime, error_code: str | None = None) -> AgentTurnRecord:
        if status not in {AgentTurnStatus.COMPLETED, AgentTurnStatus.FAILED}:
            raise ValueError("turn may only finish as completed or failed")
        with self._connect() as conn:
            row = conn.execute(
                """
                update agent_turns set
                    status = %s,
                    error_code = %s,
                    lease_owner = null,
                    lease_expires_at = null,
                    completed_at = %s,
                    updated_at = %s
                where turn_id = %s and user_id = %s
                  and status = 'running' and lease_owner = %s
                returning turn_id, thread_id, run_id, user_id, idempotency_key,
                          request_hash, status, attempt, lease_owner, lease_expires_at,
                          error_code, created_at, updated_at, started_at, completed_at
                """,
                (status.value, error_code, now, now, turn_id, user_id, worker_id),
            ).fetchone()
        if row is None:
            raise TurnLeaseMismatch(turn_id)
        return _turn_from_row(row)

    def commit_turn_result(self, *, turn: AgentTurnRecord, worker_id: str,
                           state: RunState, expected_version: int,
                           blocks: list[AgentMessageBlock], events: list[AgentEvent],
                           now: datetime) -> AgentTurnRecord:
        if (state.run_id, state.user_id, state.thread_id) != (turn.run_id, turn.user_id, turn.thread_id):
            raise ValueError("turn checkpoint does not match run ownership")
        if state.version != expected_version + 1:
            raise ValueError("turn checkpoint version is not the next run version")
        Jsonb = _jsonb_type()
        with self._connect() as conn:
            updated = conn.execute(
                """
                update agent_runs set
                    thread_id = %s,
                    active_profile = %s,
                    state = %s,
                    step_no = %s,
                    plan_id = %s,
                    plan_hash = %s,
                    pending_approval_id = %s,
                    focus = %s,
                    model_calls = %s,
                    tool_calls = %s,
                    status = %s,
                    version = %s,
                    updated_at = %s
                where run_id = %s and user_id = %s and version = %s
                returning run_id
                """,
                (
                    state.thread_id,
                    state.active_profile.value,
                    state.state.value,
                    state.step_no,
                    state.plan_id,
                    state.plan_hash,
                    state.pending_approval_id,
                    Jsonb(state.focus.model_dump(mode="json")),
                    state.model_calls,
                    state.tool_calls,
                    state.status.value,
                    expected_version + 1,
                    now,
                    state.run_id,
                    state.user_id,
                    expected_version,
                ),
            ).fetchone()
            if updated is None:
                raise StateConflict(f"expected version {expected_version}")
            if blocks:
                conn.execute(
                    """
                    insert into agent_messages (
                        message_id, thread_id, run_id, user_id, role, blocks, created_at
                    ) values (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        f"assistant-{turn.turn_id}",
                        turn.thread_id,
                        turn.run_id,
                        turn.user_id,
                        AgentMessageRole.ASSISTANT.value,
                        Jsonb([block.model_dump(mode="json") for block in blocks]),
                        now,
                    ),
                )
            for event in events:
                if event.run_id != turn.run_id or event.user_id != turn.user_id:
                    raise ValueError("turn event does not match run ownership")
                conn.execute(
                    """
                    insert into agent_events (
                        event_id, run_id, user_id, step_no, event_type, payload, created_at
                    ) values (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        event.event_id,
                        event.run_id,
                        event.user_id,
                        state.step_no,
                        event.event_type,
                        Jsonb(event.payload),
                        now,
                    ),
                )
            finished = conn.execute(
                """
                update agent_turns set
                    status = 'completed',
                    error_code = null,
                    lease_owner = null,
                    lease_expires_at = null,
                    completed_at = %s,
                    updated_at = %s
                where turn_id = %s and user_id = %s
                  and status = 'running' and lease_owner = %s
                returning turn_id, thread_id, run_id, user_id, idempotency_key,
                          request_hash, status, attempt, lease_owner, lease_expires_at,
                          error_code, created_at, updated_at, started_at, completed_at
                """,
                (now, now, turn.turn_id, turn.user_id, worker_id),
            ).fetchone()
            if finished is None:
                raise TurnLeaseMismatch(turn.turn_id)
        return _turn_from_row(finished)

    @contextmanager
    def worker_slot(self) -> Iterator[bool]:
        conn = self._connect()
        conn.autocommit = True
        acquired = bool(conn.execute(
            "select pg_try_advisory_lock(hashtext('omicsprism-agent-worker-global'))"
        ).fetchone()[0])
        try:
            yield acquired
        finally:
            if acquired:
                conn.execute("select pg_advisory_unlock(hashtext('omicsprism-agent-worker-global'))")
            conn.close()

    def save_input_bundle(self, bundle: AgentInputBundleRecord) -> None:
        self.get_thread(thread_id=bundle.thread_id, user_id=bundle.user_id)
        with self._connect() as conn:
            row = conn.execute(
                """
                insert into agent_input_bundles (
                    bundle_id, thread_id, user_id, status, expires_at, created_at
                ) values (%s, %s, %s, %s, %s, %s)
                on conflict (bundle_id) do update set
                    status = excluded.status,
                    expires_at = excluded.expires_at
                where agent_input_bundles.user_id = excluded.user_id
                returning bundle_id
                """,
                (
                    bundle.bundle_id,
                    bundle.thread_id,
                    bundle.user_id,
                    bundle.status.value,
                    bundle.expires_at,
                    bundle.created_at,
                ),
            ).fetchone()
        if row is None:
            raise AgentResourceNotFound(bundle.bundle_id)

    def get_input_bundle(self, *, bundle_id: str, user_id: str) -> AgentInputBundleRecord:
        with self._connect() as conn:
            row = conn.execute(
                """
                select bundle_id, thread_id, user_id, status, expires_at, created_at
                from agent_input_bundles where bundle_id = %s and user_id = %s
                """,
                (bundle_id, user_id),
            ).fetchone()
        if row is None:
            raise AgentResourceNotFound(bundle_id)
        return AgentInputBundleRecord.model_validate(dict(zip(
            ("bundle_id", "thread_id", "user_id", "status", "expires_at", "created_at"), row,
        )))

    def append_input_file(self, item: AgentInputFileRecord) -> None:
        self.get_input_bundle(bundle_id=item.bundle_id, user_id=item.user_id)
        with self._connect() as conn:
            conn.execute(
                """
                insert into agent_input_files (
                    file_id, bundle_id, user_id, field, filename, storage_key,
                    checksum, content_type, size_bytes, created_at
                ) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    item.file_id,
                    item.bundle_id,
                    item.user_id,
                    item.field,
                    item.filename,
                    item.storage_key,
                    item.checksum,
                    item.content_type,
                    item.size_bytes,
                    item.created_at,
                ),
            )

    def list_input_files(self, *, bundle_id: str, user_id: str) -> list[AgentInputFileRecord]:
        self.get_input_bundle(bundle_id=bundle_id, user_id=user_id)
        with self._connect() as conn:
            rows = conn.execute(
                """
                select file_id, bundle_id, user_id, field, filename, storage_key,
                       checksum, content_type, size_bytes, created_at
                from agent_input_files
                where bundle_id = %s and user_id = %s
                order by created_at, file_id
                """,
                (bundle_id, user_id),
            ).fetchall()
        fields = (
            "file_id", "bundle_id", "user_id", "field", "filename", "storage_key",
            "checksum", "content_type", "size_bytes", "created_at",
        )
        return [AgentInputFileRecord.model_validate(dict(zip(fields, row))) for row in rows]

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise RuntimeError("Install psycopg[binary]>=3.1.18 to use PostgreSQL storage") from exc
        return psycopg.connect(self.database_url)


def _thread_from_row(row) -> AgentThreadRecord:
    fields = ("thread_id", "user_id", "title", "current_run_id", "status", "version", "created_at", "updated_at")
    return AgentThreadRecord.model_validate(dict(zip(fields, row)))


def _message_from_row(row) -> AgentMessageRecord:
    fields = ("message_id", "thread_id", "run_id", "user_id", "role", "blocks", "created_at")
    return AgentMessageRecord.model_validate(dict(zip(fields, row)))


def _turn_values(turn: AgentTurnRecord) -> tuple[Any, ...]:
    return (
        turn.turn_id,
        turn.thread_id,
        turn.run_id,
        turn.user_id,
        turn.idempotency_key,
        turn.request_hash,
        turn.status.value,
        turn.attempt,
        turn.lease_owner,
        turn.lease_expires_at,
        turn.error_code,
        turn.created_at,
        turn.updated_at,
        turn.started_at,
        turn.completed_at,
    )


def _turn_from_row(row) -> AgentTurnRecord:
    fields = (
        "turn_id", "thread_id", "run_id", "user_id", "idempotency_key", "request_hash",
        "status", "attempt", "lease_owner", "lease_expires_at", "error_code",
        "created_at", "updated_at", "started_at", "completed_at",
    )
    return AgentTurnRecord.model_validate(dict(zip(fields, row)))


def _jsonb_type():
    try:
        from psycopg.types.json import Jsonb
    except ImportError as exc:  # pragma: no cover - dependency guard
        raise RuntimeError("Install psycopg[binary]>=3.1.18 to use PostgreSQL storage") from exc
    return Jsonb


def _constraint_name(exc: Exception) -> str | None:
    diagnostic = getattr(exc, "diag", None)
    return getattr(diagnostic, "constraint_name", None)
