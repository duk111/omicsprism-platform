# Agent Inventory

This inventory started in Phase 0 and is updated when extraction work changes
the runtime ownership map. It is documentation only, not a runtime registry.

## Baseline

The first backend baseline was run with the repository virtual environment:

```text
.venv\Scripts\python.exe -m pytest backend/tests -q
210 passed, 5 failed, 15 errors, 6 skipped
```

The failures/errors predate Phase 0 work (Windows temporary-directory access,
existing bundle/system-facts assertions, and the already committed workspace
changes). Phase 0 does not alter product code to hide those failures.

## Backend Agent Modules

| File | Lines | Public symbols / role | Main importers | Expected v3 outcome |
| --- | ---: | --- | --- | --- |
| `__init__.py` | 104 | Package exports for stores, typed profiles, resolver, validation, model, router, runtime and tools | `backend.agent_worker`, tests, API bootstrap | `extract`; remove legacy exports in Phase 5 |
| `api.py` | 683 | `create_agent_router`, ownership helpers, thread/turn/bundle endpoints | `backend.app.main`, API tests | `replace` wiring in Phase 4; delete approval endpoint in Phase 5 |
| `approvals.py` | 347 | `ApprovalGate`, in-memory/JSON/Postgres approval stores | `api.py`, `bootstrap.py`, `runtime.py`, `tools.py`, tests | `delete in Phase 5` |
| `audit.py` | 114 | `AgentEventStore`, safe payload checks, Postgres event store | `runtime.py`, `bootstrap.py`, worker/tests | `keep` with Phase 5 audit reduction |
| `bootstrap.py` | 44 | `AgentApiContext`, `create_agent_api_context` | app startup and API tests | `replace` graph wiring in Phase 4 |
| `context.py` | 269 | `MinimalContextBuilder`, `build_input_summaries`, conversation summaries | `runtime.py`, worker, context tests | `extract`; reuse profile/context limits |
| `dataset_profile.py` | 167 | `MatrixProfile`, `MetadataProfile`, `DatasetProfile`, `build_dataset_profiles` | `tools.py`, dataset profile tests | `keep`; typed extraction from existing inspection |
| `eval.py` | 573 | `EvalAssembly`, `EvalRunner`, golden-case loading | CLI/tests | `replace` by Phase 6 domain eval |
| `fingerprint.py` | 58 | `compute_input_fingerprint`; bounded owner/dataset/profile identity hash | `validation.py`, Phase 2 tests | `keep`; reconnect to confirmation/resume in Phase 4 |
| `grounding.py` | 144 | `EvidenceGrounder`, `GroundedAnswerPipeline` | `runtime.py`, grounding tests | `keep` (do-not-touch) |
| `model.py` | 467 | `ModelAdapter`, structured adapters, scripted/vLLM adapters | `agent_worker.py`, runtime, model tests | `extract`; reconnect to graph decisions |
| `plans.py` | 141 | `PlanStore`, plan hashing, in-memory/JSON/Postgres stores | `api.py`, runtime, tools, eval/tests | `delete in Phase 5` |
| `param_resolver.py` | 625 | Typed proposal/contrast/DEG/DEM/GMA contracts and deterministic resolution | `runtime.py`, `validation.py`, Phase 2 tests | `keep`; reconnect to Analysis node in Phase 4 |
| `policy.py` | 56 | `PolicyGuard`, profile tool authorization | `tools.py`, runtime, policy tests | `simplify` with graph capability boundaries |
| `product_store.py` | 969 | Thread/message/turn/input bundle persistence and worker lease | API, worker, bootstrap, product tests | `keep` business records; delete lease-only paths in Phase 5 |
| `router.py` | 205 | `RuleRouter`, `ModelRouter` and route heuristics | worker, runtime, eval, router tests | `delete in Phase 5`; replace with typed graph decision |
| `runtime.py` | 1594 | `ProductionRunCoordinator`, fixture coordinator, resolver compatibility adapter, orchestration helpers | worker, API bootstrap, eval, end-to-end tests | `delete in Phase 5` coordinator after graph reconnect |
| `schemas.py` | 909 | Graph-adjacent contracts, run/plan/approval/tool/message models | all Agent modules, API/frontend contracts, tests | `extract` typed v3 contracts; remove legacy schemas in Phase 5 |
| `store.py` | 161 | `StateStore`, in-memory/Postgres run state persistence | API, runtime, worker, state tests | `replace` with graph checkpointer plus retained thread/message/job data |
| `tools.py` | 748 | `AgentToolRuntime`, six legacy tools, input inspection, result evidence | runtime, worker, tool/input tests | `extract` profile/evidence services; four-tool surface in Phase 5 |
| `validator.py` | 67 | `DecisionValidator`, invalid model decision checks | runtime, worker, validator tests | `replace` with typed graph decision validation |
| `validation.py` | 187 | `DatasetRef`, `Issue`, `ContrastPreview`, `ValidationReport`, deterministic preflight adapter | Phase 2 tests; future Analysis node | `keep`; ordinary Python service, never an Agent Tool |
| `verifier.py` | 108 | `AnswerVerifier` numeric/citation/causal checks | grounding pipeline, verifier tests | `keep` (do-not-touch) |

## Legacy Tests To Retire

The following test areas are marked in source with `# DEPRECATED-BY: phase-5`:

- RuleRouter and fallback routing: `test_phase2_control_plane.py`,
  `test_phase7_s5_router.py`.
- ApprovalGate and PlanStore lifecycle: the matching sections in
  `test_phase2_control_plane.py` and `test_phase3_tools.py`.
- ProductionRunCoordinator end-to-end state machine: the lifecycle sections in
  `test_agent_end_to_end.py` and `test_phase6b_production_runtime.py`.
- Worker lease/recovery behavior: `test_phase6b_worker.py` and
  `test_phase6b_db_worker.py`.

Result evidence, ownership, grounding, and verifier assertions in mixed test
files remain active until their graph/domain replacements exist.

The contrast cases that encode the current two-level inference limitation are
marked `# BEHAVIOR-CHANGE-IN: phase-2.2`; they are not treated as Phase 5
deletions.

## Frontend Agent Coupling

- Approval API calls and `plan_hash` payloads live in
  `frontend/src/copilot/agentApi.ts` (`decideApproval`) and are rendered by
  `frontend/src/copilot/MessageBlocks.tsx` / `CopilotPage.tsx`.
- Legacy approval, plan, and run state types are currently hand-maintained in
  `frontend/src/api-types.ts`; they are not generated at build time.
- `frontend/scripts/generate-api-types.mjs` exists for future regeneration from
  the OpenAPI schema. Phase 4.9 and Phase 5.7 are the only authorized frontend
  touch points for the graph interrupt contract and approval removal.
- Existing frontend tests with legacy coupling include
  `frontend/src/copilot/MessageBlocks.test.tsx` and
  `frontend/e2e/copilot.spec.ts`.
