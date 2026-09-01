# ADR 0002: Job Completion Recovery

- Status: accepted
- Date: 2026-09-01
- Scope: Phase 4 durable Agent recovery after analysis Jobs

## Decision

Use a durable Job wait record plus a Job completion outbox event. When a graph
submits a Job, the runtime will persist a wait record bound to the original
Agent context (`thread_id`, `user_id`, `turn_id`, `run_id`, `trace_id`, and
`job_id`). When the Job reaches a terminal state, the Job transaction will
create one outbox event. A reconciler delivers that event at least once and
creates one continuation Agent turn on the same thread. The continuation turn
resumes the existing LangGraph checkpoint and performs result QA or emits a
typed failure/cancellation message.

The continuation is a normal durable Agent turn, not a human-facing
clarification/confirmation interrupt. Its idempotency key is derived from the
stable completion event id, so duplicate delivery cannot create a second
continuation turn or final message.

## Alternatives considered

### LangGraph typed wait interrupt

An internal typed interrupt would keep the graph paused until a system event
resumed it. This is attractive inside one runtime, but it makes event delivery,
ownership checks, cancellation, and observability depend on a special resume
channel in addition to the existing Agent turn contract. It also risks exposing
system waiting as a user interrupt in the current API.

### Job completion continuation turn (selected)

The existing API-to-Redis-to-runtime path already provides durable turns,
ownership checks, idempotency, retries, and trace identifiers. A continuation
turn therefore needs fewer new execution paths while still allowing the graph
checkpoint to remain the source of workflow state.

## Persistence and transaction boundaries

The Job worker writes the Job terminal state and its outbox event in the same
Postgres transaction. The event is considered publishable only after that
transaction commits. Redis publication is deliberately outside the transaction
and is retried by reconciliation. The reconciler atomically claims an
unpublished event, publishes a continuation work item, and records the
continuation idempotency key; a crash between publication and acknowledgement
is safe because the turn store and its unique idempotency constraint absorb the
duplicate.

If a deployment uses the local JSON Job repository, no cross-process recovery
guarantee is claimed; production recovery requires the Postgres repository.

## Idempotency, cancellation, and stale events

- One event id is derived from `(job_id, terminal status)` and is unique in the
  outbox. Repeated terminal writes do not create another event.
- A wait can transition only from `waiting` to one terminal handling outcome.
  Duplicate, late, or non-terminal events are acknowledged without changing
  the checkpoint or creating a turn.
- Ownership is checked against both the wait record and the Job owner before
  any continuation is queued. A mismatched user or thread is rejected without
  revealing whether the Job exists.
- Cancelling the Agent wait marks the wait cancelled and prevents a future
  continuation. Cancelling the Job produces a normal terminal cancellation
  event; if the cancellation races with delivery, the wait state wins and the
  event is acknowledged without a user-visible duplicate.
- Job timeout is represented as a terminal failure outcome with an explicit
  timeout error code. It is not an untyped runtime exception.

## Observability

Every wait and completion event carries the original `thread_id`, `user_id`,
`turn_id`, `run_id`, `trace_id`, and `job_id`. Reconciliation records delivery
attempts and error codes. No raw CSV, credentials, storage keys, or full model
prompt is stored in the event payload.

## Consequences

The first implementation adds durable contracts and tables before changing
graph behavior. The implementation now wires terminal Job writes,
reconciliation, continuation graph input, SSE waiting state, wait cancellation,
and a worker-side timeout watchdog against these contracts. Existing
clarification and confirmation resume APIs remain unchanged.
