# Phase 8 Acceptance Evidence

This is the reproducible acceptance entry point for the current v3 runtime.
It runs deterministic local fault drills and a bounded queue capacity probe;
it does not contact production services or a live model.

## Run

```text
.venv\Scripts\python.exe scripts/run_phase8_acceptance.py --json --output .test-tmp\phase8-3-acceptance.json
```

The command runs these drills:

- runtime/process interruption resumes from the durable checkpoint;
- a transient database-style connection failure is requeued with bounded retry;
- duplicate Job completion delivery reuses one continuation turn.

The capacity section uses a fixed number of concurrent producers and work items
and reports enqueue wall time, reserve P95, and queue residue after draining.
Use `--concurrency` and `--items` to repeat a larger local probe, keeping both
within the script's bounded limits.

This local probe is not a substitute for a production load test. Production
capacity depends on Redis, PostgreSQL, vLLM latency, artifact storage, and the
number of runtime replicas.

## Phase 8.4 Demo And Delivery Boundaries

[`DEMO.md`](DEMO.md) is the five-minute walkthrough for the cloud API,
multi-turn correction, HITL confirmation, asynchronous Job continuation,
evidence-backed response, local MCP read-only contract, and trace/Eval evidence.
The current frontend does not contain a trace/Eval panel, so the walkthrough
uses the existing REST/stream interfaces, a safe `agent_trace_events` query,
and the repository evaluation scripts instead of claiming an unavailable UI.

[`LIMITATIONS_AND_ROADMAP.md`](LIMITATIONS_AND_ROADMAP.md) records the current
single-runtime, local-MCP, deterministic-evaluation, cost, streaming, storage,
and security boundaries together with the next hardening steps.
