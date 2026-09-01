# ADR 0001: Agent Runtime Boundary

- Status: accepted
- Date: 2026-09-01
- Scope: OmicsPrism Platform v3 Agent execution and production deployment

## Decision

The Agent graph has one production execution boundary: `backend.agent_runtime`.
The cloud API does not invoke LangGraph, the model, or Agent tools. It validates
the HTTP request, writes the user message and durable turn record to PostgreSQL,
and publishes an `AgentTurnWorkItem` to Redis. A runtime process consumes that
item on the compute server and executes the graph with the shared PostgreSQL
checkpointer.

The runtime uses the compute host's local vLLM endpoint through host networking
(`127.0.0.1:18000` by default). The analysis worker is a separate consumer of
the ordinary Job queue and is not an Agent graph runtime.

## Context

The system is deployed across a cloud server and a laboratory compute server.
The cloud server provides the externally reachable API and persistent services;
the compute server provides GPU inference and analysis execution. Agent HITL
resumption requires checkpoints shared across processes, so the production graph
uses `PostgresSaver`; `InMemorySaver` is test-only.

## Responsibilities

### Cloud API

- Authenticate and authorize requests.
- Persist threads, turns, user messages, and lifecycle status.
- Enqueue Agent work on `OMICS_PRISM_AGENT_QUEUE`.
- Read durable Agent messages and expose them over HTTP.
- Never call `graph.invoke`, vLLM, or Agent tools for production turns.

### Agent runtime

- Claim and execute queued Agent turns.
- Load the graph with the production `tool_executor`.
- Read and write LangGraph checkpoints in PostgreSQL.
- Call the local model endpoint and deterministic read/write tools.
- Persist assistant messages, errors, and HITL interrupt state.

### Analysis worker

- Consume `OMICS_PRISM_REDIS_QUEUE` (`omicsprism:jobs`).
- Execute DEG, DEM, and GMA jobs.
- Update Job records and artifacts.
- Do not execute the Agent graph.

## Delivery and ownership

Agent delivery is at-least-once. Redis moves an item to a recoverable processing
list; a runtime restart requeues stale items. Turn state and graph checkpoints
are durable in PostgreSQL. Duplicate delivery is handled by turn lifecycle
checks and deterministic Job idempotency keys. Every graph operation and tool
request remains scoped to the authenticated user and project. The current Redis
processing-list protocol expects one active Agent runtime consumer for an Agent
queue; do not scale that queue horizontally without replacing or extending its
claim/recovery semantics and proving duplicate-delivery behavior.

## Deployment topology

```text
Browser
  -> cloud nginx/API
       -> PostgreSQL product store (threads, turns, messages)
       -> Redis Agent queue ----------------> compute agent-runtime
                                               -> LangGraph/PostgreSQL checkpoints
                                               -> local vLLM (127.0.0.1:18000)
                                               -> Redis analysis Job queue
  -> Redis analysis Job queue -------------> compute analysis worker
                                               -> PostgreSQL Job records
                                               -> MinIO inputs and artifacts
```

The compute server must be able to reach the cloud server's PostgreSQL, Redis,
and MinIO endpoints. vLLM does not need to be reachable from the public
internet. Keep the Agent queue name and database credentials identical on both
servers. Use [`AGENT_RUNTIME_DEPLOYMENT.md`](AGENT_RUNTIME_DEPLOYMENT.md) for
the operational commands.

## Constraints and non-goals

- Do not add a second production Agent runtime inside the API.
- Do not run the removed legacy `agent-worker` container alongside v3 runtime.
- Do not use `InMemorySaver` for production or rely on API process memory for HITL.
- This decision does not define MCP transport, tracing, token accounting, or
  conversation summarization; those are later P0 phases.

## Rejected alternatives

1. **Graph inside API**: couples request availability to model latency and cannot
   reliably resume HITL state across API processes or restarts.
2. **Legacy `agent-worker`**: executes the pre-v3 control flow and does not share
   the current graph/checkpointer/tool contract.
3. **Separate checkpoint store per process**: loses durable cross-process state
   and permits divergent graph turns.

## Consequences

The API can return `202 Accepted` quickly and scale independently from GPU
inference. Operators must monitor both Redis queues and the runtime logs, and
must deploy matching code and environment settings on both servers. A database,
Redis, or MinIO network failure can delay or retry Agent work, but cannot silently
fall back to an API-local graph.

## Source references

- `backend/app/agent/api.py`
- `backend/app/agent/queue.py`
- `backend/app/agent/runtime.py`
- `backend/app/agent/bootstrap.py`
- `backend/agent_runtime.py`
- `backend/worker.py`
- `docker-compose.agent-runtime.yml`
- `AGENT_RUNTIME_DEPLOYMENT.md`
