# P0 Stage 1: Trace and Model Usage Baseline

## Scope

Stage 1 adds a safe, durable observability baseline to the existing production
control plane. The production execution boundary remains unchanged:

```text
Browser -> API -> Redis agent-turn queue -> agent-runtime -> LangGraph/vLLM/tools
                                                    -> analysis job queue
```

The API still persists and queues work only. The independent `agent-runtime`
continues to execute the graph, model, read-only tools, and confirmed Job
submission.

## Delivered Contract

- Each newly created turn has one `trace_id`. It is carried by the persisted
  turn and messages, the Redis work item, graph state, model context, tools,
  confirmed Job submission, runtime logs, and assistant finalization.
- Duplicate deliveries and idempotency replays use the original persisted trace
  identity; they do not create a new turn or trace.
- `agent_trace_events` stores owner-bound, append-only trace summaries for
  queued/started/completed/failed turns, model calls, tool calls, and Job
  submission. Queries require both `trace_id` and `user_id`. Deleting a thread
  deletes its trace events with the rest of its conversation data.
- Trace records contain identifiers, hashes, versions, outcome, latency, retry
  count, token usage, tool name/schema hash, and Job id only. They never retain
  prompt text, CSV content, tool arguments, object storage paths, DSNs, or
  credentials. Model rejection logs similarly omit raw model output.
- `VllmGraphModel` reads OpenAI-compatible provider usage. Reported
  prompt/completion/total/cached token values are persisted and propagated into
  the graph budget. Missing usage is explicitly represented as `unknown` with
  null trace fields; it is not estimated from serialized output length. Model
  step limits still bound such requests.
- The graph records separate reported prompt, completion, and total token
  counters, alongside count of calls whose usage is unknown.
- `OpenTelemetryTraceObserver` emits safe spans and latency/event metrics through
  the vendor-neutral `opentelemetry-api` only. No Langfuse, LangSmith, exporter,
  or external observability service is required for application behavior.

## Migrations and Deployment

Apply migrations in filename order with the existing migration container or
script. This stage introduces:

1. `012_agent_trace_events.sql`: the `agent_trace_events` table, ownership-bound
   indexes, constraints, and `omics_app` privileges.
2. `013_agent_trace_id_columns.sql`: non-null `trace_id` columns for
   `agent_turns` and `agent_messages`, deterministic legacy values for existing
   rows, and lookup indexes.

Rebuild both the cloud API image and compute-server `agent-runtime` image after
pulling this commit, because both components create or consume trace metadata.
The analysis worker does not execute the graph and does not need a trace-specific
runtime change. The new pinned backend dependency is
`opentelemetry-api==1.44.0`.

## Verification

Executed in the local repository after implementation:

```text
.venv\Scripts\python.exe -m pytest backend/tests -q
202 passed, 2 skipped

.venv\Scripts\python.exe scripts/run_agent_eval.py
Overall: PASS

npm --prefix frontend run test
4 files passed, 13 tests passed

npm --prefix frontend run build
Succeeded
```

The eval report still exposes pre-existing grounded-QA quality gaps: citation
validity and numeric consistency were 0.667, unsupported-claim rate was 0.333,
and one hallucinated entity was detected. Overall eval remains PASS because the
existing release criteria do not yet gate those metrics. Stage 2 must turn these
known negative cases into expanded Eval v2 scenarios and explicit release gates.

## Remaining Boundaries

- A trace query API/UI and cost report are intentionally not introduced in this
  stage; Phase 2 and Phase 6 consume the owner-bound store contract.
- No vendor exporter is configured. Deploy an OTLP exporter only after collector
  endpoint, retention, and tenant access controls are defined.
- Wall-clock timeout, cancellation policy, and cost calculations remain Phase 6
  work.
- Automatic Job-completion continuation and complete multi-turn context remain
  later phases and are not implied by this trace baseline.
