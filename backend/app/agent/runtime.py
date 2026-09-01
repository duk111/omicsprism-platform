from __future__ import annotations

import logging
from datetime import datetime, timezone
from time import perf_counter

from langgraph.types import Command
from psycopg import OperationalError

from .bootstrap import AgentApiContext
from .context import ContextAssembler, build_recent_messages
from .graph import GraphInterrupt, GraphState, JobLookupRequest, JobRef, JobSummary
from ..observability import log_context
from .product_store import AgentResourceNotFound, TurnConflict
from .job_events import AgentJobWaitStatus
from .reconciliation import AgentJobEventReconciler
from .queue import AgentTurnQueue, AgentTurnWorkItem
from .schemas import (
    AgentMessageBlock,
    AgentMessageRecord,
    AgentMessageRole,
    AgentErrorBlock,
    AgentTextBlock,
    AgentTurnStatus,
    AgentTurnRecord,
)
from .message_blocks import job_block, job_block_from_summary, text_block
from ..models import JobStatus


LOG = logging.getLogger("omicsprism.platform.agent_runtime")
_MAX_TRANSIENT_RETRIES = 1
_RUNTIME_ERROR_MESSAGE = "The request could not be completed. Please try again."


class AgentRuntime:
    """Consumes durable Agent turns and executes the graph outside HTTP."""

    def __init__(
        self,
        context: AgentApiContext,
        queue: AgentTurnQueue,
        *,
        reconciler: AgentJobEventReconciler | None = None,
    ) -> None:
        self.context = context
        self.queue = queue
        self.reconciler = reconciler or AgentJobEventReconciler(
            context.product_store,
            queue,
        )

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
            self._record_turn_event(
                "turn.started",
                item,
                run_id=turn.run_id,
                retry_count=max(0, turn.attempt - 1),
            )
            config = {"configurable": {"thread_id": item.thread_id}}
            if item.continuation is not None and self._continuation_cancelled(item):
                self._cancel_claimed_turn(item, turn)
                self.queue.ack(raw_item)
                return
            if item.continuation is not None:
                self._run_continuation(item, config, turn.run_id)
            elif item.input is not None or item.state is not None:
                self._run_start(item, config, turn.attempt, turn.run_id)
            else:
                self._run_resume(item, config)
            if item.continuation is not None and self._continuation_cancelled(item):
                self._cancel_claimed_turn(item, turn)
                self.queue.ack(raw_item)
                return
            finalized = self._finalize(item, turn)
            if finalized and item.continuation is not None:
                self._complete_job_wait(item)
            if finalized:
                self._record_turn_event(
                    "turn.completed",
                    item,
                    run_id=turn.run_id,
                    outcome="completed",
                    latency_ms=round((perf_counter() - started_at) * 1000, 3),
                    retry_count=max(0, turn.attempt - 1),
                )
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
            self.reconciler.reconcile_once()
            raw_item = self.queue.reserve()
            if raw_item is None:
                continue
            self.run_once(raw_item)

    def _run_start(
        self,
        item: AgentTurnWorkItem,
        config: dict,
        attempt: int,
        run_id: str,
    ) -> None:
        if item.state is None and item.input is None:
            raise ValueError("start work item is missing input")
        if item.input is not None:
            snapshot = self.context.graph.get_state(config)
            values = getattr(snapshot, "values", None)
            if isinstance(values, GraphState):
                current = values
            elif values:
                try:
                    current = GraphState.model_validate(values)
                except (TypeError, ValueError):
                    current = None
            else:
                current = None
            if attempt > 1 and current is not None:
                if _snapshot_interrupts(snapshot):
                    return
                if current.turn_id == item.turn_id:
                    if getattr(snapshot, "next", ()):  # retry after a transient failure
                        self._invoke_graph(item, None, config)
                    return
            merged = self._merge_turn_input(item, current, run_id)
            self.context.graph.update_state(config, merged.model_dump(mode="json"))
            self._invoke_graph(item, merged.model_dump(mode="json"), config)
            return
        assert item.state is not None
        if attempt == 1:
            state_values = item.state.model_dump(mode="json")
            self.context.graph.update_state(
                config,
                state_values,
            )
            # Passing the new state is required when a thread already has a
            # completed checkpoint; invoke(None, ...) would observe its empty
            # `next` tuple and skip the graph entirely.
            self._invoke_graph(item, state_values, config)
            return
        snapshot = self.context.graph.get_state(config)
        if _snapshot_interrupts(snapshot):
            return
        if getattr(snapshot, "next", ()):
            self._invoke_graph(item, None, config)
            return
        if not _checkpoint_matches(snapshot, item.state):
            state_values = item.state.model_dump(mode="json")
            self.context.graph.update_state(config, state_values)
            self._invoke_graph(item, state_values, config)

    def _merge_turn_input(
        self,
        item: AgentTurnWorkItem,
        current: GraphState | None,
        run_id: str,
    ) -> GraphState:
        turn_input = item.input
        if turn_input is None:
            raise ValueError("turn input is missing")
        if current is None:
            current = GraphState(
                thread_id=item.thread_id,
                user_id=item.user_id,
                trace_id=item.trace_id,
                turn_id=item.turn_id,
                run_id=run_id,
                user_message=turn_input.message,
            )
        if current.thread_id != item.thread_id or current.user_id != item.user_id:
            raise ValueError("graph checkpoint ownership mismatch")
        focus = current.focus
        version = current.version
        recent_jobs = list(current.recent_jobs)
        existing_memory = current.conversation_memory
        corrections = list(existing_memory.user_corrections)
        if _is_explicit_correction(turn_input.message):
            corrections.append(turn_input.message[:400])
        if turn_input.focus_job_ids:
            focus = focus.model_copy(update={"in_scope_job_ids": list(turn_input.focus_job_ids)})
            version += 1
            for job_id in turn_input.focus_job_ids:
                recent_jobs = [item for item in recent_jobs if item.job_id != job_id]
                from .graph import JobRef
                recent_jobs.append(JobRef(job_id=job_id, owner_id=item.user_id))
        has_new_inputs = bool(turn_input.dataset_profiles)
        inherited_profiles = (
            list(current.dataset_profiles)
            if not has_new_inputs and self._can_inherit_dataset_profiles(current, item)
            else []
        )
        merged = current.model_copy(update={
            "trace_id": item.trace_id,
            "turn_id": item.turn_id,
            "run_id": run_id,
            "user_message": turn_input.message,
            "focus": focus,
            "version": version,
            "dataset_profiles": (
                list(turn_input.dataset_profiles)
                if has_new_inputs
                else inherited_profiles
            ),
            "active_input_bundle_id": (
                turn_input.input_bundle_id if has_new_inputs
                else current.active_input_bundle_id if inherited_profiles else None
            ),
            "recent_jobs": recent_jobs[-20:],
            # Per-turn state must never leak into the next user message.
            "decision": None,
            "response_text": None,
            "response_blocks": [],
            "clarification_answer": None,
            "resolved_request": None,
            "validation_report": None,
            "job_summary": None,
            "grounded_answer": None,
            "pending_plan": None,
            "pending_interrupt": None,
            "tool_observations": [],
            "step_budget": type(current.step_budget)(),
            "conversation_memory": existing_memory.model_copy(update={
                "user_corrections": corrections[-12:],
                "preferences": dict(focus.preferences),
            }),
        })
        try:
            messages = self.context.product_store.list_messages(
                thread_id=item.thread_id,
                user_id=item.user_id,
                limit=100,
            )
            recent, summary = build_recent_messages(messages)
            ledger_memory = ContextAssembler().assemble(merged).conversation_memory
            merged = merged.model_copy(update={
                "recent_messages": recent,
                "conversation_summary": summary or current.conversation_summary,
                "conversation_memory": ledger_memory,
            })
        except Exception:
            # Message history is an optimization for prompting; checkpoint
            # ownership and the turn input remain sufficient to execute safely.
            LOG.warning("unable to refresh bounded conversation context", extra={
                "event": "agent.context.refresh_failed",
                "thread_id": item.thread_id,
            })
        return merged

    def _can_inherit_dataset_profiles(
        self,
        current: GraphState,
        item: AgentTurnWorkItem,
    ) -> bool:
        bundle_id = current.active_input_bundle_id
        if bundle_id is None:
            # Checkpoints created before Stage 3 have no bundle reference.
            # Preserve their bounded profile references for compatibility.
            return True
        try:
            bundle = self.context.product_store.get_input_bundle(
                bundle_id=bundle_id,
                user_id=item.user_id,
            )
        except AgentResourceNotFound:
            return False
        return (
            bundle.thread_id == item.thread_id
            and bundle.status.value == "active"
            and datetime.now(timezone.utc) < bundle.expires_at
        )

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
        self._invoke_graph(item, Command(resume=value), config)

    def _run_continuation(
        self,
        item: AgentTurnWorkItem,
        config: dict,
        run_id: str,
    ) -> None:
        event = item.continuation
        if event is None:
            raise ValueError("continuation work item is missing its event")
        snapshot = self.context.graph.get_state(config)
        state = GraphState.model_validate(snapshot.values)
        if state.thread_id != item.thread_id or state.user_id != item.user_id:
            raise ValueError("graph checkpoint ownership mismatch")
        if state.run_id != event.run_id:
            raise ValueError("Job completion event run does not match graph checkpoint")
        # The graph receives a bounded system message. Job status and error
        # details are read deterministically by the next graph node; no model
        # supplied event fields are trusted for ownership or artifacts.
        message = (
            f"System Job event: {event.job_id} reached {event.status.value}."
            if event.status.value == "succeeded"
            else f"System Job event: {event.job_id} reached {event.status.value}."
        )
        updated = state.model_copy(update={
            "turn_id": item.turn_id,
            "run_id": run_id,
            "trace_id": item.trace_id,
            "user_message": message,
            # Completion events are the authoritative ownership-bound Job
            # reference. A later user turn may have changed current_job on the
            # same thread, so continuation must never resolve against stale
            # checkpoint focus or ask the model to guess which Job completed.
            "current_job": JobRef(job_id=event.job_id, owner_id=event.user_id),
            "recent_jobs": [
                *[job for job in state.recent_jobs if job.job_id != event.job_id],
                JobRef(job_id=event.job_id, owner_id=event.user_id),
            ][-20:],
            "response_text": None,
            "response_blocks": [],
            "decision": None,
            "pending_interrupt": None,
            "job_summary": None,
            "grounded_answer": None,
            "tool_observations": [],
        })
        if event.status.value != "succeeded":
            outcome = "failed" if event.status.value == "failed" else "cancelled"
            detail = f" ({event.error_code})" if event.error_code else ""
            updated = updated.model_copy(update={
                "response_text": (
                    f"Analysis job {event.job_id} {outcome}{detail}. "
                    "No result interpretation was generated."
                ),
                "response_blocks": [
                    text_block(
                        f"Analysis job {event.job_id} {outcome}{detail}. "
                        "No result interpretation was generated."
                    ),
                    job_block(
                        event.job_id,
                        JobStatus(event.status.value),
                        progress=100 if event.status.value == "succeeded" else 0,
                    ),
                ],
            })
            self.context.graph.update_state(config, updated.model_dump(mode="json"))
            return
        if self.context.job_reader is not None:
            try:
                summary = self.context.job_reader(JobLookupRequest(
                    user_id=event.user_id,
                    job_id=event.job_id,
                ))
                if summary.owner_id != event.user_id or summary.job_id != event.job_id:
                    raise ValueError("completion Job reader returned an invalid Job")
                updated = updated.model_copy(update={"job_summary": summary})
                if not summary.artifacts:
                    updated = updated.model_copy(update={
                        "response_text": (
                            f"Analysis job {event.job_id} succeeded, but no result artifacts are available for interpretation."
                        ),
                        "response_blocks": _completion_notice_blocks(
                            summary,
                            f"Analysis job {event.job_id} succeeded, but no result artifacts are available for interpretation.",
                        ),
                    })
                    self.context.graph.update_state(config, updated.model_dump(mode="json"))
                    return
            except LookupError:
                updated = updated.model_copy(update={
                    "response_text": (
                        f"Analysis job {event.job_id} succeeded, but its result metadata is unavailable."
                    ),
                    "response_blocks": [text_block(
                        f"Analysis job {event.job_id} succeeded, but its result metadata is unavailable."
                    )],
                })
                self.context.graph.update_state(config, updated.model_dump(mode="json"))
                return
        self.context.graph.update_state(config, updated.model_dump(mode="json"))
        self._invoke_graph(item, updated.model_dump(mode="json"), config)

    def _complete_job_wait(self, item: AgentTurnWorkItem) -> None:
        event = item.continuation
        if event is None:
            return
        status_map = {
            "succeeded": AgentJobWaitStatus.COMPLETED,
            "failed": AgentJobWaitStatus.FAILED,
            "cancelled": AgentJobWaitStatus.CANCELLED,
        }
        self.context.product_store.complete_job_wait(
            job_id=event.job_id,
            user_id=event.user_id,
            continuation_turn_id=item.turn_id,
            status=status_map[event.status.value],
            now=datetime.now(timezone.utc),
        )

    def _continuation_cancelled(self, item: AgentTurnWorkItem) -> bool:
        event = item.continuation
        if event is None:
            return False
        try:
            wait = self.context.product_store.get_job_wait(
                job_id=event.job_id,
                user_id=event.user_id,
            )
        except AgentResourceNotFound:
            return False
        return wait.status in {
            AgentJobWaitStatus.CANCELLED,
            AgentJobWaitStatus.EXPIRED,
        }

    def _cancel_claimed_turn(
        self,
        item: AgentTurnWorkItem,
        turn: AgentTurnRecord,
    ) -> None:
        try:
            self.context.product_store.cancel_turn(
                turn_id=turn.turn_id,
                user_id=turn.user_id,
                now=datetime.now(timezone.utc),
                error_code="agent_wait_cancelled",
            )
        except TurnConflict:
            # A concurrent delivery or API cancellation already finalized it.
            pass

    def _invoke_graph(self, item: AgentTurnWorkItem, *args):
        with log_context(
            trace_id=item.trace_id,
            user_id=item.user_id,
            project_id=item.thread_id,
        ):
            return self.context.graph.invoke(*args)

    def _finalize(self, item: AgentTurnWorkItem, turn: AgentTurnRecord) -> bool:
        config = {"configurable": {"thread_id": item.thread_id}}
        snapshot = self.context.graph.get_state(config)
        state = GraphState.model_validate(snapshot.values)
        if state.thread_id != item.thread_id or state.user_id != item.user_id:
            raise ValueError("graph checkpoint ownership mismatch")
        interrupts = _snapshot_interrupts(snapshot)
        if interrupts:
            return False
        if not state.response_text:
            raise ValueError("completed graph state is missing response_text")
        blocks: list[AgentMessageBlock] = list(state.response_blocks)
        if not blocks:
            blocks = [AgentTextBlock(text=state.response_text)]
        message = AgentMessageRecord(
            message_id=f"assistant-{item.turn_id}",
            thread_id=item.thread_id,
            run_id=turn.run_id,
            trace_id=item.trace_id,
            user_id=item.user_id,
            role=AgentMessageRole.ASSISTANT,
            blocks=blocks,
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
        return True

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
                    trace_id=item.trace_id,
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
                self._record_turn_event(
                    "turn.failed",
                    item,
                    run_id=current.run_id,
                    outcome="failed",
                    error_code=error_code,
                    retry_count=max(0, current.attempt - 1),
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

    def _record_turn_event(
        self,
        event_type: str,
        item: AgentTurnWorkItem,
        *,
        run_id: str | None = None,
        outcome: str | None = None,
        latency_ms: float | None = None,
        retry_count: int = 0,
        error_code: str | None = None,
    ) -> None:
        recorder = self.context.trace_recorder
        if recorder is None:
            return
        recorder.turn_event(
            event_type=event_type,  # type: ignore[arg-type]
            trace_id=item.trace_id,
            thread_id=item.thread_id,
            turn_id=item.turn_id,
            run_id=run_id,
            user_id=item.user_id,
            outcome=outcome,
            latency_ms=latency_ms,
            retry_count=retry_count,
            error_code=error_code,
        )


def _completion_notice_blocks(summary: JobSummary, text: str) -> list[AgentMessageBlock]:
    blocks: list[AgentMessageBlock] = [text_block(text)]
    job = job_block_from_summary(summary)
    if job is not None:
        blocks.append(job)
    return blocks


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


def _is_explicit_correction(message: str) -> bool:
    normalized = message.casefold()
    markers = ("改为", "更正", "修正", "instead", "change ", "replace ")
    return any(marker in normalized for marker in markers)
