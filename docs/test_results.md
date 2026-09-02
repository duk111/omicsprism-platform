# OmicsPrism Refactor Test Results

This document records the test and evaluation results observed during the
Phase 0-6 refactor. Commands are shown relative to the
`omicsprism-platform` repository root and use the repository virtual
environment.

## Verification Environment

```text
Python: .venv\Scripts\python.exe
Backend tests: .venv\Scripts\python.exe -m pytest backend/tests -q
Eval runner: .venv\Scripts\python.exe scripts/run_agent_eval.py
```

Each Phase 6 task was committed and pushed separately. The working tree was
clean after each commit.

## Phase Summary

| Phase / task | Targeted result | Full backend result | Notes |
| --- | --- | --- | --- |
| Phase 0 baseline | Not applicable | `210 passed, 5 failed, 15 errors, 6 skipped` | Historical baseline recorded in `inventory.md`; failures predated Phase 0. |
| Phase 1 | Result count not retained in the final task log | Result count not retained | Typed `DatasetProfile` extraction completed and committed as `46d3cf9`. |
| Phase 2 | Result count not retained in the final task log | Result count not retained | Resolver, validation, multi-factor ambiguity, and fingerprint work completed and committed as `aa62d0d`. |
| Phase 3 | Result count not retained in the final task log | Result count not retained | Figure JSON result querying and grounding contracts completed and committed as `0159617`. |
| Phase 4 | Result count not retained in the final task log | Result count not retained | LangGraph orchestration and API/frontend interrupt contract completed through 4.9. |
| Phase 5 | Result count not retained in the final task log | Result count not retained | Legacy control plane removal completed through 5.7. |
| 6.1 | `13 passed` | `150 passed, 2 skipped, 3 failed` | Parameter inference cases. |
| 6.2 | `16 passed` | `153 passed, 2 skipped, 3 failed` | Ambiguity cases and deterministic clarification metrics. |
| 6.3 | `25 passed` | `156 passed, 2 skipped, 3 failed` | Grounded QA cases and citation/evidence checks. |
| 6.4 | `14 passed` | `253 passed, 2 skipped` | Configurable release gate, cross-user zero gate, memory/grounding thresholds, and latency/cost budgets. |
| 6.5 | `15 passed` | `162 passed, 2 skipped, 3 failed` | Eval runner tests and domain evaluation runner. |
| Schema cleanup | `54 passed` | `150 passed, 2 skipped, 3 failed` | Removed legacy decision/context contracts; the three known bundle inheritance failures remain. |
| State/lease cleanup | `34 passed` | `150 passed, 2 skipped, 3 failed` | Removed zombie run-state fields and turn leases; the same three known bundle inheritance failures remain. |
| Bundle inheritance fix | `5 passed` | `153 passed, 2 skipped` | Fixed in-memory bundle timestamp filtering; all backend tests now pass. |
| StateStore/checkpointer replacement | `44 passed` | `154 passed, 2 skipped` | GraphState focus/version now use the LangGraph checkpointer; custom StateStore and `agent_runs` storage were removed. |
| Phase 4 durable Job waits | `32 passed` | `233 passed, 2 skipped` | Added public Job-wait SSE/API projections, wait cancellation with continuation-race protection, and worker timeout watchdog with explicit `job_timeout`. |
| Phase 4 result continuation closure | `20 passed` | `237 passed, 2 skipped` | Completion continuations now bind the terminal Job into the checkpoint, prefetch the ownership-validated Job summary, avoid model calls when successful Jobs expose no artifacts, and cover grounded evidence plus duplicate delivery. |
| Phase 5 structured response blocks | `31 passed` | `237 passed, 2 skipped` | Typed graph output now persists text, Job, and evidence blocks unchanged through runtime, API, SSE, and the existing frontend renderer. Job URLs are derived only from ownership-bound Job ids; evidence retains artifact, checksum, and row ids. |
| Phase 6.1 feedback-to-eval review loop | `26 passed` | `243 passed, 2 skipped` | Assistant-message feedback is ownership-bound to its turn and trace. Negative feedback or a correction creates a redacted pending-review candidate; helpful feedback removes any prior candidate. Candidates can only be exported by the internal review script and have no public approval or golden-set API. |

| Frontend unit tests | `4 files, 13 passed` | Not applicable | `npm --prefix frontend run test`. |
| Frontend production build | Successful | Not applicable | `npm --prefix frontend run build`; Vite emitted a large-chunk warning only. |
| Phase 6.1 frontend feedback tests | `5 files, 16 passed` | Not applicable | `npm --prefix frontend run test -- --run`; includes typed feedback API and feedback-control coverage. |
| Frontend E2E after contract cleanup | `6 passed` | Not applicable | `npm --prefix frontend run test:e2e`; Vite logged expected job-progress proxy errors because no backend was running during the mocked browser run. |
| Production deployment audit | Passed static checks | Not applicable | Compose YAML parsed; API model settings are injected and Uvicorn is fixed to one worker for `InMemorySaver`. Docker CLI was unavailable in the local verification environment. |

The Phase 6 targeted suites were run with the relevant prior suites. For
example, 6.3 included the Phase 4 grounding and JSON query tests, and 6.4
included the complete graph flow suite.

## Phase 6.1 Feedback Review Loop

The implementation accepts helpful or unhelpful feedback only for an owned
assistant message. The repository resolves the message to its owned turn and
trace rather than trusting any client-supplied identifiers. An unhelpful
rating requires a failure category; optional correction text is normalized
before storage.

Only an unhelpful rating or correction text creates a review candidate. Before
the candidate is persisted, message summaries are redacted for connection
strings, secrets, object-storage references, email addresses, IP addresses,
local paths, and table-shaped CSV content. The persisted candidate starts as
pending_review. The browser has no candidate, approval, rejection, or
golden-set route. Updating feedback to helpful removes the candidate; deleting
the conversation cascades the feedback and candidate records.

Internal reviewers may obtain anonymized pending candidates with:

    .venv\Scripts\python.exe scripts\export_agent_eval_candidates.py --output pending-agent-eval-candidates.json

The export intentionally excludes user_id, thread_id, turn_id, message_id,
trace_id, and feedback_id. It does not approve candidates or write evaluation
fixtures. A reviewer must make any golden-set promotion as a separate,
auditable change.

Observed verification:

    .venv\Scripts\python.exe -m pytest backend/tests -q --basetemp .pytest-phase6-feedback-release
    243 passed, 2 skipped

    npm --prefix frontend run test -- --run
    5 files, 16 passed

    npm --prefix frontend run build
    Successful; existing Plotly bundle-size warning only

## Historical Full-Test Failures (Resolved)

The following three failures appeared in earlier Phase 6 full backend runs:

```text
backend/tests/test_fix02_bundle_inheritance.py::test_consecutive_bundles_inherit_previous_files
backend/tests/test_fix02_bundle_inheritance.py::test_same_field_override_not_duplicate
backend/tests/test_fix02_bundle_inheritance.py::test_expired_bundle_not_inherited
```

Failure:

```text
TypeError: '<' not supported between instances of 'str' and 'datetime.datetime'
backend/app/agent/product_store.py:284
```

They were caused by comparing a JSON-serialized `created_at` string with a
`datetime` in the in-memory product store. The query now compares timestamps
after Pydantic deserialization, matching the existing expiration check. The
bundle regression suite and the full backend suite pass without skipped or
weakened assertions.

## Phase 6 Domain Evaluation

Command:

```text
.venv\Scripts\python.exe scripts/run_agent_eval.py
```

Observed output:

```text
Parameter Inference
  cases: 3
  resolved rate: 1.000
  field accuracy: 1.000
  pair accuracy: 1.000
  full contrast exact match: 1.000
Ambiguity
  cases: 4
  clarification recall: 1.000
  false-positive rate: 0.000
  illegal auto-run count: 0
Grounded QA
  cases: 3
  citation validity: 0.667
  numeric consistency: 0.667
  unsupported-claim rate: 0.333
  hallucinated entity count: 1
Capability Isolation
  status: PASS
  detail: 3 passed
Overall: PASS
```

The Grounded QA baseline intentionally includes one negative hallucinated
entity case. The non-zero unsupported-claim rate and hallucinated entity count
demonstrate that the evaluator detects unsupported claims instead of reporting
an artificially perfect score. See `eval_baseline.md` for the metric meaning.

## Test Scope Notes

- Ownership, grounding, capability boundaries, deterministic resolution, and
  validation-before-execution checks remained covered by targeted regression
  suites.
- The Phase 6 runner uses Pydantic-validated reports and runs the capability
  isolation regression as a subprocess.
- Frontend test/build results were run after the Phase 6 task log and are
  recorded in the Phase Summary table.

```text
npm --prefix frontend run test
npm --prefix frontend run build
```

## Phase 6.2 Version, Cost, and Latency Reporting

Eval v2 reports now carry the graph, prompt, provider, and model identity used
for each run. They also aggregate model/tool call counts, P50/P95 wall-clock
latency, and token usage. Since the current vLLM path does not provide a cloud
price card, its cost is reported as `unknown`; no unrelated provider pricing is
applied. A matching explicit JSON price card enables USD input/output cost
calculation, including cached-input pricing when configured.

The deterministic CI runner identifies its fixture model as `recorded-fixture`,
so a real vLLM price card cannot be accidentally applied to recorded tests.

Generate and persist a report:

```text
.venv\\Scripts\\python.exe scripts/run_agent_eval_v2.py --json --output eval-baseline.json
```

Compare two persisted reports without manual spreadsheets:

```text
.venv\\Scripts\\python.exe scripts/compare_agent_eval_v2.py eval-baseline.json eval-candidate.json
```

The comparison includes version changes, pass@1 and consistency deltas, P95
turn/model/tool latency deltas, model/tool call deltas, illegal automatic
execution deltas, and cost/token deltas. Token and cost deltas remain `null`
when either report has unknown cost status.

## Phase 6.3 Runtime Reliability

Agent runtime reliability controls are now active in the production entrypoint:

- each turn has a wall-clock deadline (`OMICS_PRISM_AGENT_TURN_TIMEOUT_SECONDS`);
- cancellation is checked before and after graph execution and prevents finalization;
- transient database failures use a bounded retry count with exponential backoff,
  a cap, and jitter;
- timed-out or exhausted work is acknowledged into a Redis DLQ named
  `<agent-queue>:dlq` (the in-memory queue exposes the same contract for tests);
- timeout error blocks are explicitly non-retryable, while ordinary runtime
  failures retain the existing retryable error projection.

Verified with:

```text
.venv\\Scripts\\python.exe -m pytest backend/tests/test_agent_runtime.py -q --basetemp=.test-tmp\\phase-6-3-runtime
.venv\\Scripts\\python.exe -m pytest backend/tests -q --basetemp=.test-tmp\\phase-6-3-full
```

## Phase 6.4 Release Gate

The release gate is now driven by the strict, versioned EvalGateConfig model.
Safety invariants remain fixed at zero/one where the product risk requires it;
memory, unsupported claims, latency and cost use explicit thresholds. Unknown
local-model cost remains unknown unless require_cost_known is enabled.
Persisted reports include the configuration, and report comparison identifies
gate and budget regressions.

The default configuration is
backend/app/agent/fixtures/eval_gate_config.json. Override it with:

    .venv\\Scripts\\python.exe scripts/run_agent_eval_v2.py --json --gate-config backend/app/agent/fixtures/eval_gate_config.json --output eval-baseline.json

Verified with:

    .venv\\Scripts\\python.exe -m pytest backend/tests/test_agent_eval_v2.py -q --basetemp=.test-tmp\\phase-6-4-gate
    14 passed

## Phase 7.1 Capability Registry

Phase 7.1 introduced the shared, ownership-bound `CapabilityRegistry`. The
production Web Agent dispatches its six bounded read operations through this
registry, which validates strict request/response schemas and uses one
non-enumerating error for unknown or unauthorized capabilities. No MCP SDK,
remote transport, analysis preparation, or Job submission capability is
enabled yet; the boundary and scope are recorded in `ADR_0003_MCP_CAPABILITY_BOUNDARY.md`.

Verified with:

```text
.venv\Scripts\python.exe -m pytest backend/tests/test_agent_capabilities.py backend/tests/test_agent_production_wiring.py backend/tests/test_agent_readonly_tools.py -q --basetemp=.test-tmp\phase-7-1-registry
16 passed
```

## Phase 7.2 In-Process MCP Adapter

Phase 7.2 adds the official `mcp==2.1.1` SDK as a backend dependency and an
in-process `CapabilityMCPServer`. The adapter registers the same six read-only
capabilities as the Web Agent, publishes the registry's strict request and
response schemas, binds an explicit trusted principal, and performs strict
validation before SDK dispatch. No MCP network transport is started.

Verified with:

```text
.venv\Scripts\python.exe -m pytest backend/tests/test_agent_capabilities.py backend/tests/test_agent_mcp_adapter.py -q --basetemp=.test-tmp\phase-7-2-mcp
10 passed
```

The full backend suite reached 263 passed and 2 skipped (2 existing FastAPI
deprecation warnings).

## Phase 7.3 MCP Trace Integration

MCP calls now optionally share the existing `TraceRecorder` contract. A trusted
`MCPTraceContext` binds the call to an existing Agent thread and must match the
principal subject. Successful, invalid, and not-visible calls emit safe
`tool.call` events with client transport, schema hash, latency, and normalized
result code; arguments and raw data are excluded.

Verified with:

```text
.venv\Scripts\python.exe -m pytest backend/tests/test_agent_mcp_adapter.py -q --basetemp=.test-tmp\phase-7-3-mcp
9 passed
```

## Phase 8.1 Architecture And Threat Model

Added auditable final-delivery documents for the implemented v3 topology:
`ARCHITECTURE.md` records cloud/API, compute runtime, LangGraph checkpoint,
analysis worker, outbox, and MCP boundaries; `THREAT_MODEL.md` records assets,
trust boundaries, controls, residual risks, and operational requirements.
README links both documents from the repository overview.

## Phase 8.2 Evaluation Scenario Coverage

The deterministic Eval v2 fixture now contains 50 unique scenarios: 47 Agent
quality cases across multi-turn memory, ambiguity, confirmation, grounding,
and capability isolation, plus 3 evaluator self-tests. The expanded ambiguity
set includes Chinese, English, and mixed-language omics questions while keeping
recorded model responses reproducible.

Verified with:

```text
.venv\Scripts\python.exe -m pytest backend/tests/test_agent_eval_v2.py -q --basetemp=.test-tmp\phase-8-2-eval
14 passed
.venv\Scripts\python.exe scripts/run_agent_eval_v2.py --json --trials 1 --output .test-tmp\phase8-2-eval.json
release_gate.passed=true, quality.case_count=41, quality.pass_at_1=1.0
```

## Phase 8.3 Fault And Capacity Acceptance

`PHASE_8_ACCEPTANCE.md` and `scripts/run_phase8_acceptance.py` provide one
repeatable local acceptance command. It executes runtime interruption recovery,
transient database-style retry, and duplicate Job completion drills, then runs a
bounded concurrent in-memory queue probe with enqueue and reserve P95 metrics.

Verified with:

```text
.venv\Scripts\python.exe scripts/run_phase8_acceptance.py --json --items 64 --concurrency 8 --basetemp .pytest-phase8-3-drills --output .test-tmp/phase8-3-acceptance.json
fault_drills: 3 passed
capacity: concurrency=8, items=64, enqueued=64, pending_after_drain=0, processing_after_drain=0
.venv\Scripts\python.exe -m pytest backend/tests/test_phase8_acceptance.py -q --basetemp=.test-tmp\phase-8-3-script
2 passed
```

The full backend regression after the Phase 8.3 additions is `267 passed, 2
skipped, 2 warnings`.
