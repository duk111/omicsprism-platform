# Known Limitations And Roadmap

This document is the honest boundary of the current v3 delivery. The project
can be described as a recoverable, evaluable, evidence-constrained omics Agent;
it must not yet be described as a complete multi-tenant production platform.

## Current limitations

| Area | Current behavior | Operational consequence |
| --- | --- | --- |
| Agent scheduling | Redis Agent delivery/recovery is designed for one active runtime consumer | Do not scale `agent-runtime` horizontally without replacing the claim/recovery protocol and re-running duplicate-delivery tests. |
| MCP transport | The official SDK is used through an in-process, read-only adapter; no HTTP listener is enabled | A remote MCP client cannot connect until authentication, quota, rate limiting, audit, and trace propagation are implemented. |
| Trace and Eval UI | Trace summaries and Eval v2 reports are available through Postgres and CLI; there is no frontend trace/eval panel | Operators use the SQL/CLI steps in `DEMO.md`; the UI gap is explicit rather than hidden behind a simulated panel. |
| Live-model evaluation | The 50-case fixture and Phase 8 acceptance runner are deterministic local checks | They prove contract and recovery behavior, not live vLLM quality, GPU capacity, or production SLOs. |
| Token cost | Provider usage is recorded when reported; local vLLM has no configured price card | Cost remains `unknown` unless an explicit matching price card is supplied. Unknown is never treated as zero. |
| Streaming latency | The API stream is durable polling/SSE; there is no token streaming | TTFT means the first visible durable event, not the first generated token. |
| Local storage | JSON storage is useful for development and fixtures | Cross-process Agent recovery and Job completion guarantees require Postgres/Redis/MinIO deployment. |
| Security scope | Ownership checks, bounded capabilities, and non-enumerating errors are implemented | Remote identity federation, centralized secrets, WAF policy, and a full production compliance program remain outside this repository. |
| Analysis capacity | DEG/DEM/GMA execute on the separate compute worker | Long analyses depend on the compute host, vLLM latency, artifact storage, and configured timeout; the local capacity probe is not a load test. |

## Near-term roadmap

### 1. Operations surface

Add a role-protected trace/evaluation view that renders safe event summaries,
turn latency, queue age, model usage, release-gate failures, and links to the
existing feedback/eval-candidate workflow. Keep raw prompts, CSV rows, secrets,
and storage keys out of the view.

### 2. Authenticated MCP transport

Introduce Streamable HTTP only after selecting and deploying a real JWT/OAuth
principal boundary. Reuse `CapabilityRegistry`, shared quotas, rate limits,
audit, and trace context. Keep `prepare_analysis` and `submit_analysis` closed
until short-lived plan tokens and confirmation replay checks are demonstrated.

### 3. Runtime scheduling and scale

Replace the single-consumer Redis recovery assumptions with a lease/claim
protocol or a scheduler that proves at-least-once delivery, fairness, and
bounded duplicate execution under multiple runtime replicas.

### 4. Live evaluation and telemetry

Run controlled multi-trial evaluations against each approved model/prompt/graph
version, export OpenTelemetry data to an operational backend, and publish P50/
P95 queue, model, tool, and end-to-end latency together with real usage and
cost. Keep deterministic CI fixtures as a regression gate.

### 5. Product hardening

Add retention and deletion policies for checkpoints, traces, and artifacts;
document backup/restore drills; and validate the cloud-to-compute firewall,
credential rotation, and timeout budgets on a staging deployment.

## Release posture

The current release is suitable for a controlled demonstration and internal
review when deployed with the documented cloud/compute split. Before exposing
new write-capable MCP or multiple Agent runtimes, the roadmap items above need
security and recovery evidence, not only unit tests.
