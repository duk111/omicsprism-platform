from __future__ import annotations

from collections import deque
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .graph import DatasetProfileRef, GraphResumeRequest, GraphState
from .job_events import AgentJobCompletionEvent


class AgentTurnInput(BaseModel):
    """Only the new user input supplied for one graph turn."""

    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=4000)
    input_bundle_id: str | None = Field(default=None, min_length=1, max_length=200)
    dataset_profiles: list[DatasetProfileRef] = Field(default_factory=list, max_length=6)
    # An empty list means that the existing thread focus remains unchanged.
    focus_job_ids: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _profiles_require_bundle_reference(self) -> "AgentTurnInput":
        if self.dataset_profiles and self.input_bundle_id is None:
            raise ValueError("dataset profiles require an input bundle reference")
        return self


class AgentTurnWorkItem(BaseModel):
    """Durable unit of Agent graph work published by the API."""

    model_config = ConfigDict(extra="forbid")

    turn_id: str = Field(min_length=1, max_length=200)
    thread_id: str = Field(min_length=1, max_length=200)
    trace_id: str = Field(default="trace-local", min_length=1, max_length=200)
    user_id: str = Field(min_length=1, max_length=200)
    input: AgentTurnInput | None = None
    # Legacy start payload retained while already queued messages drain.
    state: GraphState | None = None
    resume: GraphResumeRequest | None = None
    continuation: AgentJobCompletionEvent | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def _one_operation(self) -> "AgentTurnWorkItem":
        operations = sum(item is not None for item in (self.input, self.state, self.resume, self.continuation))
        if operations != 1:
            raise ValueError("work item must contain exactly one graph operation")
        if self.input is not None and self.idempotency_key is not None:
            raise ValueError("idempotency key is only valid for approved resume")
        if self.continuation is not None and self.idempotency_key is not None:
            raise ValueError("idempotency key is not accepted on Job continuation work")
        if self.state is not None and (
            self.state.thread_id != self.thread_id
            or self.state.turn_id not in {"turn-local", self.turn_id}
            or self.state.trace_id not in {"trace-local", self.trace_id}
            or self.state.user_id != self.user_id
        ):
            raise ValueError("work item state does not match its owner")
        if self.continuation is not None and (
            self.continuation.thread_id != self.thread_id
            or self.continuation.user_id != self.user_id
        ):
            raise ValueError("continuation event does not match its owner")
        if self.resume is not None:
            if self.resume.kind == "confirmation" and self.resume.approve is True:
                if self.idempotency_key is None:
                    raise ValueError("approved resume requires an idempotency key")
            elif self.idempotency_key is not None:
                raise ValueError("idempotency key is only valid for approved resume")
        return self


class AgentTurnQueue(Protocol):
    """At-least-once queue boundary for Agent graph turns."""

    def enqueue(self, item: AgentTurnWorkItem) -> None:
        ...

    def recover_pending(self) -> None:
        ...

    def reserve(self, timeout_seconds: int = 5) -> str | None:
        ...

    def ack(self, raw_item: str) -> None:
        ...

    def retry(self, raw_item: str) -> None:
        ...

    def dead_letter(self, raw_item: str, *, reason: str) -> None:
        ...


class InMemoryAgentTurnQueue:
    """Small deterministic queue used by unit tests and local graph fixtures."""

    def __init__(self) -> None:
        self.pending: deque[str] = deque()
        self.processing: list[str] = []
        self.dead_letters: list[dict[str, str]] = []

    def enqueue(self, item: AgentTurnWorkItem) -> None:
        self.pending.append(item.model_dump_json())

    def recover_pending(self) -> None:
        while self.processing:
            self.pending.appendleft(self.processing.pop())

    def reserve(self, timeout_seconds: int = 5) -> str | None:
        del timeout_seconds
        if not self.pending:
            return None
        raw_item = self.pending.popleft()
        self.processing.append(raw_item)
        return raw_item

    def ack(self, raw_item: str) -> None:
        try:
            self.processing.remove(raw_item)
        except ValueError:
            pass

    def retry(self, raw_item: str) -> None:
        self.ack(raw_item)
        self.pending.append(raw_item)

    def dead_letter(self, raw_item: str, *, reason: str) -> None:
        self.ack(raw_item)
        self.dead_letters.append({"reason": reason, "item": raw_item})


class RedisAgentTurnQueue:
    """Reliable Redis queue with a recoverable processing list.

    A single Agent runtime consumer is expected for a queue. Duplicate delivery
    after recovery is intentional and is handled by turn and Job idempotency.
    """

    def __init__(
        self,
        redis_url: str,
        queue_name: str = "omicsprism:agent-turns",
    ) -> None:
        self.redis_url = redis_url
        self.queue_name = queue_name
        self.processing_queue_name = f"{queue_name}:processing"
        self.dead_letter_queue_name = f"{queue_name}:dlq"
        self._client = None

    @property
    def client(self):
        if self._client is None:
            try:
                import redis
            except ImportError as exc:  # pragma: no cover - dependency guard
                raise RuntimeError("Install redis>=5.0.0 to use the Agent queue") from exc
            self._client = redis.Redis.from_url(
                self.redis_url,
                decode_responses=True,
                socket_connect_timeout=10,
                socket_timeout=30,
                health_check_interval=30,
                retry_on_timeout=True,
            )
        return self._client

    def enqueue(self, item: AgentTurnWorkItem) -> None:
        self.client.rpush(self.queue_name, item.model_dump_json())

    def recover_pending(self) -> None:
        stale = self.client.lrange(self.processing_queue_name, 0, -1)
        if not stale:
            return
        pipeline = self.client.pipeline(transaction=True)
        pipeline.rpush(self.queue_name, *stale)
        pipeline.delete(self.processing_queue_name)
        pipeline.execute()

    def reserve(self, timeout_seconds: int = 5) -> str | None:
        return self.client.brpoplpush(
            self.queue_name,
            self.processing_queue_name,
            timeout=timeout_seconds,
        )

    def ack(self, raw_item: str) -> None:
        self.client.lrem(self.processing_queue_name, 1, raw_item)

    def retry(self, raw_item: str) -> None:
        pipeline = self.client.pipeline(transaction=True)
        pipeline.rpush(self.queue_name, raw_item)
        pipeline.lrem(self.processing_queue_name, 1, raw_item)
        pipeline.execute()

    def dead_letter(self, raw_item: str, *, reason: str) -> None:
        import json
        from datetime import datetime, timezone

        payload = json.dumps({
            "reason": reason,
            "failed_at": datetime.now(timezone.utc).isoformat(),
            "item": raw_item,
        }, ensure_ascii=False)
        pipeline = self.client.pipeline(transaction=True)
        pipeline.rpush(self.dead_letter_queue_name, payload)
        pipeline.lrem(self.processing_queue_name, 1, raw_item)
        pipeline.execute()

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            close()
