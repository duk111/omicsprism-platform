from __future__ import annotations

import logging
from datetime import datetime, timezone

from .job_events import AgentJobCompletionEvent
from .product_store import AgentResourceNotFound, AgentProductStore
from .queue import AgentTurnQueue, AgentTurnWorkItem


LOG = logging.getLogger("omicsprism.platform.agent_reconciliation")


class AgentJobEventReconciler:
    """Publishes durable Job completion events to the existing Agent queue."""

    def __init__(self, store: AgentProductStore, queue: AgentTurnQueue) -> None:
        self.store = store
        self.queue = queue

    def reconcile_once(self, *, limit: int = 20) -> int:
        delivered = 0
        for event in self.store.list_pending_job_events(limit=limit):
            if self._deliver(event):
                delivered += 1
        return delivered

    def _deliver(self, event: AgentJobCompletionEvent) -> bool:
        now = datetime.now(timezone.utc)
        try:
            turn = self.store.prepare_job_continuation(event, now=now)
            if turn is None:
                self.store.mark_job_event_published(event_id=event.event_id, now=now)
                return True
            self.queue.enqueue(AgentTurnWorkItem(
                turn_id=turn.turn_id,
                thread_id=event.thread_id,
                trace_id=event.trace_id,
                user_id=event.user_id,
                continuation=event,
            ))
            self.store.mark_job_event_published(event_id=event.event_id, now=now)
            LOG.info(
                "published Agent Job continuation",
                extra={
                    "event": "agent.job_event.published",
                    "event_id": event.event_id,
                    "job_id": event.job_id,
                    "thread_id": event.thread_id,
                    "turn_id": turn.turn_id,
                },
            )
            return True
        except AgentResourceNotFound:
            # The owning thread may have been deleted after the Job completed.
            # Acknowledge the no-longer-visible event without leaking its Job.
            try:
                self.store.mark_job_event_published(event_id=event.event_id, now=now)
            except AgentResourceNotFound:
                pass
            return True
        except Exception as exc:
            try:
                self.store.mark_job_event_failed(
                    event_id=event.event_id,
                    error=type(exc).__name__,
                    now=now,
                )
            except Exception:
                LOG.exception(
                    "unable to record Agent Job event delivery failure",
                    extra={"event": "agent.job_event.failure_persist", "event_id": event.event_id},
                )
            LOG.warning(
                "Agent Job event delivery deferred",
                extra={
                    "event": "agent.job_event.deferred",
                    "event_id": event.event_id,
                    "job_id": event.job_id,
                    "error_code": type(exc).__name__,
                },
            )
            return False
