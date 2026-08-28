from __future__ import annotations

import logging
from datetime import datetime, timezone
from time import perf_counter

from langgraph.types import Command
from psycopg import OperationalError

from .bootstrap import AgentApiContext
from .graph import GraphInterrupt, GraphState
from .product_store import AgentResourceNotFound, TurnConflict
from .queue import AgentTurnQueue, AgentTurnWorkItem
from .schemas import (
    AgentMessageRecord,
    AgentMessageRole,
    AgentErrorBlock,
    AgentTextBlock,
    AgentTurnStatus,
    AgentTurnRecord,
)


LOG = logging.getLogger("omicsprism.platform.agent_runtime")
_MAX_TRANSIENT_RETRIES = 1
_RUNTIME_ERROR_MESSAGE = "The request could not be completed. Please try again."


class AgentRuntime:
    """Consumes durable Agent turns and executes the graph outside HTTP."""

    def __init__(self, context: AgentApiContext, queue: AgentTurnQueue) -> None:
        self.context = context
        self.queue = queue

    def run_once(self, raw_item: str) -> None:
        try:
            item = AgentTurnWorkItem.model_validate_json(raw_item)
        except Exception:
            LOG.error("discarding invalid Agent work item", extra={"event": "agent.work.invalid"})
            self.queue.ack(raw_item)
            return

        started_at = perf_counter()
        try:
            turn = self.context.product_store.get_turn(
                turn_id=item.turn_id,
                user_id=item.user_id,
            )
            if turn.status is AgentTurnStatus.CANCELLED:
                self.queue.ack(raw_item)
                return
            turn = self.context.product_store.claim_turn(
                turn_id=item.turn_id,
                user_id=item.user_id,
                now=datetime.now(timezone.utc),
            )
            if turn.status in {
                AgentTurnStatus.COMPLETED,
                AgentTurnStatus.FAILED,
                AgentTurnStatus.CANCELLED,
            }:
                self.queue.ack(raw_item)
                return
            config = {"configurable": {"thread_id": item.thread_id}}
            if item.state is not None:
                self._run_start(item, config, turn.attempt)
            else:
                self._run_resume(item, config)
            self._finalize(item, turn)
            self.queue.ack(raw_item)
            LOG.info(
                "agent turn processed",
                extra={
                    "event": "agent.turn.processed",
                    "thread_id": item.thread_id,
                    "turn_id": item.turn_id,
                    "duration_ms": round((perf_counter() - started_at) * 1000, 3),
                },
            )
        except AgentResourceNotFound:
            self.queue.ack(raw_item)
        except OperationalError:
            if self._retry_transient(item, raw_item):
                LOG.warning(
                    "requeued Agent turn after transient database failure",
                    extra={
                        "event": "agent.turn.retry",
                        "thread_id": item.thread_id,
                        "turn_id": item.turn_id,
                    },
                )
                return
            self._fail(item, "agent_runtime_failed")
            self.queue.ack(raw_item)
            LOG.exception(
                "agent turn failed after transient retry",
                extra={
                    "event": "agent.turn.failed",
                    "thread_id": item.thread_id,
                    "turn_id": item.turn_id,
                },
            )
        except Exception as exc:
            self._fail(item, "agent_runtime_failed")
            self.queue.ack(raw_item)
            LOG.exception(
                "agent turn failed",
                extra={
                    "event": "agent.turn.failed",
                    "thread_id": item.thread_id,
                    "turn_id": item.turn_id,
                },
            )

    def run_forever(self) -> None:  # pragma: no cover - process entrypoint
        self.queue.recover_pending()
        while True:
            raw_item = self.queue.reserve()
            if raw_item is None:
                continue
            self.run_once(raw_item)

    def _run_start(self, item: AgentTurnWorkItem, config: dict, attempt: int) -> None:
        if item.state is None:
            raise ValueError("start work item is missing graph state")
        if attempt == 1:
            state_values = item.state.model_dump(mode="json")
            self.context.graph.update_state(
                config,
                state_values,
            )
            # Passing the new state is required when a thread already has a
            # completed checkpoint; invoke(None, ...) would observe its empty
            # `next` tuple and skip the graph entirely.
            self.context.graph.invoke(state_values, config)
            return
        snapshot = self.context.graph.get_state(config)
        if _snapshot_interrupts(snapshot):
            return
        if getattr(snapshot, "next", ()):
            self.context.graph.invoke(None, config)
            return
        if not _checkpoint_matches(snapshot, item.state):
            state_values = item.state.model_dump(mode="json")
            self.context.graph.update_state(config, state_values)
            self.context.graph.invoke(state_values, config)

    def _run_resume(self, item: AgentTurnWorkItem, config: dict) -> None:
        if item.resume is None:
            raise ValueError("resume work item is missing resume payload")
        snapshot = self.context.graph.get_state(config)
        interrupts = _snapshot_interrupts(snapshot)
        if not interrupts:
            return
        if len(interrupts) != 1 or interrupts[0].interrupt_id != item.resume.interrupt_id:
            return
        value = item.resume.model_dump(mode="json")
        value.pop("kind", None)
        value.pop("interrupt_id", None)
        if item.idempotency_key is not None:
            value["idempotency_key"] = item.idempotency_key
        self.context.graph.invoke(Command(resume=value), config)

    def _finalize(self, item: AgentTurnWorkItem, turn: AgentTurnRecord) -> None:
        config = {"configurable": {"thread_id": item.thread_id}}
        snapshot = self.context.graph.get_state(config)
        state = GraphState.model_validate(snapshot.values)
        if state.thread_id != item.thread_id or state.user_id != item.user_id:
            raise ValueError("graph checkpoint ownership mismatch")
        interrupts = _snapshot_interrupts(snapshot)
        if interrupts:
            return
        if not state.response_text:
            raise ValueError("completed graph state is missing response_text")
        message = AgentMessageRecord(
            message_id=f"assistant-{item.turn_id}",
            thread_id=item.thread_id,
            run_id=turn.run_id,
            user_id=item.user_id,
            role=AgentMessageRole.ASSISTANT,
            blocks=[AgentTextBlock(text=state.response_text)],
            created_at=datetime.now(timezone.utc),
        )
        try:
            self.context.product_store.finish_turn(
                turn_id=item.turn_id,
                user_id=item.user_id,
                status=AgentTurnStatus.COMPLETED,
                now=datetime.now(timezone.utc),
                message=message,
            )
        except TurnConflict:
            # A duplicate delivery may have completed the same turn already.
            current = self.context.product_store.get_turn(
                turn_id=item.turn_id,
                user_id=item.user_id,
            )
            if current.status is not AgentTurnStatus.COMPLETED:
                raise

    def _fail(self, item: AgentTurnWorkItem, error_code: str) -> None:
        try:
            current = self.context.product_store.get_turn(
                turn_id=item.turn_id,
                user_id=item.user_id,
            )
            if current.status is AgentTurnStatus.RUNNING:
                message = AgentMessageRecord(
                    message_id=f"assistant-{item.turn_id}",
                    thread_id=item.thread_id,
                    run_id=current.run_id,
                    user_id=item.user_id,
                    role=AgentMessageRole.ASSISTANT,
                    blocks=[AgentErrorBlock(
                        code=error_code,
                        user_message=_RUNTIME_ERROR_MESSAGE,
                        retryable=True,
                    )],
                    created_at=datetime.now(timezone.utc),
                )
                self.context.product_store.finish_turn(
                    turn_id=item.turn_id,
                    user_id=item.user_id,
                    status=AgentTurnStatus.FAILED,
                    now=datetime.now(timezone.utc),
                    message=message,
                    error_code=error_code,
                )
        except Exception:
            LOG.exception("unable to mark Agent turn failed", extra={"event": "agent.turn.fail_persist"})

    def _retry_transient(self, item: AgentTurnWorkItem, raw_item: str) -> bool:
        """Requeue one turn after a recoverable database connection reset."""

        try:
            turn = self.context.product_store.get_turn(
                turn_id=item.turn_id,
                user_id=item.user_id,
            )
            if turn.attempt > _MAX_TRANSIENT_RETRIES:
                return False
            if turn.status is AgentTurnStatus.RUNNING:
                self.context.product_store.queue_turn(
                    turn_id=item.turn_id,
                    user_id=item.user_id,
                    now=datetime.now(timezone.utc),
                )
            elif turn.status is not AgentTurnStatus.QUEUED:
                return False
            self.queue.retry(raw_item)
            return True
        except OperationalError:
            # Leave the reserved item in processing so a process restart can
            # recover it instead of acknowledging work whose turn state is
            # unknown.
            LOG.warning(
                "could not inspect Agent turn after database failure",
                extra={"event": "agent.turn.retry_deferred", "turn_id": item.turn_id},
            )
            return True
        except Exception:
            LOG.exception(
                "unable to requeue Agent turn after transient failure",
                extra={"event": "agent.turn.retry_failed", "turn_id": item.turn_id},
            )
            return False


def _snapshot_interrupts(snapshot: object) -> list[GraphInterrupt]:
    return [
        GraphInterrupt(interrupt_id=item.id, payload=item.value)
        for task in getattr(snapshot, "tasks", ())
        for item in task.interrupts
    ]


def _checkpoint_matches(snapshot: object, state: GraphState) -> bool:
    values = getattr(snapshot, "values", None)
    if not values:
        return False
    try:
        current = GraphState.model_validate(values)
    except (TypeError, ValueError):
        return False
    return current.model_dump(mode="json") == state.model_dump(mode="json")
