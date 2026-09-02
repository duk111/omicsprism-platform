# OmicsPrism Five-Minute Demo

This script demonstrates the implemented v3 path: cloud API -> Redis Agent
queue -> compute `agent-runtime` -> LangGraph/Postgres checkpoint -> local
vLLM. It assumes the stack and the `Upload example` files are already
available. Use a disposable session and dataset for the demo.

## 0:00 - Verify the split runtime

On the cloud server:

```bash
docker compose --env-file .env -p omicsprism ps
curl -fsS http://127.0.0.1:18086/health
```

On the compute server:

```bash
docker compose --env-file .env -p omicsprism \
  -f docker-compose.agent-runtime.yml ps
curl -fsS http://127.0.0.1:18000/health
```

There must be one `agent-runtime` consumer for the Agent queue and a separate
analysis worker. The API should not make a model request itself.

## 0:30 - Start a thread and upload data

The shortest path is to open the frontend, create a Copilot thread, and choose
`Upload example`. The same operation can be exercised with the API. Keep the
cookie jar for the entire session:

```bash
curl -sS -c /tmp/omicsprism-demo-cookie -b /tmp/omicsprism-demo-cookie \
  -H 'Content-Type: application/json' \
  -d '{"focus_job_ids":[]}' \
  http://127.0.0.1:18086/api/agent/threads
```

Copy `thread.thread_id` from the response. The upload endpoint is
`POST /api/agent/threads/{thread_id}/input-bundles` and accepts the CSV files
with their field roles. The browser's Upload example action is preferable for
a live demonstration because it also renders the active bundle summary.

## 1:00 - Multi-turn correction and HITL

Send an intentionally broad request first, then correct its scope in a second
turn. Every turn is asynchronous and needs a unique idempotency key:

```bash
curl -sS -c /tmp/omicsprism-demo-cookie -b /tmp/omicsprism-demo-cookie \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-turn-1' \
  -d '{"message":"Find the most significant genes","focus_job_ids":[]}' \
  http://127.0.0.1:18086/api/agent/threads/<THREAD_ID>/turns

curl -sS -c /tmp/omicsprism-demo-cookie -b /tmp/omicsprism-demo-cookie \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: demo-turn-2' \
  -d '{"message":"Compare salt and control within the same line and timepoint","focus_job_ids":[]}' \
  http://127.0.0.1:18086/api/agent/threads/<THREAD_ID>/turns
```

Read the pending state either in the UI or with:

```bash
curl -sS -b /tmp/omicsprism-demo-cookie \
  http://127.0.0.1:18086/api/agent/threads/<THREAD_ID>/pending-interrupt
```

If the plan is ambiguous, answer the clarification. If it is ready to run,
the confirmation payload contains `plan_id` and `plan_version`; the UI's Run
button submits the confirmation with a fresh idempotency key. The graph will
not create a Job before this confirmation.

## 2:00 - Observe the asynchronous Job

The confirmation returns quickly while the analysis worker handles DEG, DEM,
or GMA. Watch the thread stream or poll the durable records:

```bash
curl -N -b /tmp/omicsprism-demo-cookie \
  'http://127.0.0.1:18086/api/agent/threads/<THREAD_ID>/stream'

curl -sS -b /tmp/omicsprism-demo-cookie \
  http://127.0.0.1:18086/api/agent/threads/<THREAD_ID>/job-waits
```

The Job completion outbox creates one continuation turn. A runtime restart or
duplicate completion event is safe: the checkpoint and the stable completion
idempotency key prevent duplicate continuation turns.

## 3:30 - Evidence-backed answer

When the Job reaches a terminal state, the continuation produces an assistant
message grounded in an owned artifact. Inspect the message and artifact
metadata through the thread APIs or the normal result view:

```bash
curl -sS -b /tmp/omicsprism-demo-cookie \
  http://127.0.0.1:18086/api/agent/threads/<THREAD_ID>/messages
curl -sS -b /tmp/omicsprism-demo-cookie \
  http://127.0.0.1:18086/api/jobs/<JOB_ID>/files
```

Claims must carry artifact/checksum/row citation data. Missing or insufficient
evidence produces the explicit refusal response instead of an invented number.

## 4:15 - MCP read-only capability

MCP is currently process-local and read-only. The adapter reuses the same
ownership-bound `CapabilityRegistry`; it does not open a network listener. A
deterministic contract check is:

```powershell
.venv\Scripts\python.exe -m pytest `
  backend/tests/test_agent_mcp_adapter.py::test_mcp_call_returns_typed_structured_readonly_result -q
```

The test calls `describe_metadata` and verifies typed levels, ownership, and
strict argument validation. The full adapter contract is covered by
`backend/tests/test_agent_mcp_adapter.py`.

## 4:40 - Trace and Eval evidence

The right-hand `Trace evidence` panel shows safe summaries for the active
thread and can be refreshed after a turn. For an operator review, the same
data can be queried directly with the trace id from the turn response:

```sql
select event_type, component, name, outcome, latency_ms,
       usage_status, prompt_tokens, completion_tokens, error_code, created_at
from agent_trace_events
where trace_id = '<TRACE_ID>' and user_id = '<SESSION_ID>'
order by created_at;
```

The panel deliberately does not pretend to be a live Eval dashboard. Run the
reproducible deterministic evaluation from the repository root:

```powershell
.venv\Scripts\python.exe scripts/run_agent_eval_v2.py `
  --json --trials 1 --output .test-tmp\demo-eval.json
.venv\Scripts\python.exe scripts/run_phase8_acceptance.py `
  --json --items 64 --concurrency 8 --output .test-tmp\demo-acceptance.json
```

The reports expose quality, safety, latency, usage, release-gate, fault-drill,
and queue-residue evidence. They are deterministic local acceptance evidence,
not a claim of live-model production capacity.

## What to point out

- The model proposes a typed decision; deterministic services own validation,
  authorization, HITL, idempotency, and execution.
- The API remains responsive because Graph/LLM execution is on compute.
- A Job completion is an event-driven continuation of the same checkpoint.
- The evidence path is ownership-bound and refuses unsupported numeric claims.
- MCP exposes the shared read-only boundary only; remote authenticated MCP is
  intentionally a later phase.
