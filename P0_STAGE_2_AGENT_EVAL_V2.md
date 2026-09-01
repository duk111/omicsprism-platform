# P0 Stage 2: End-to-End Agent Eval v2

## Scope

Stage 2 adds a deterministic evaluation harness around the existing production
Agent graph. It does not change API routing, Redis queue contracts, persistent
checkpointer behavior, production tool wiring, database schema, or frontend
behavior.

Eval fixture -> isolated LangGraph -> recorded model boundary -> deterministic tools/Jobs
                           |                    |
                           +-> trace events      +-> typed graph state and HITL interrupts

The CI runner uses recorded structured model outputs. It exercises actual graph
nodes, confirmations, resume paths, tool capability boundaries, Job/result
readers, grounding, trace recording, and token reporting, without calling vLLM.
The explicitly opt-in live runner calls an OpenAI-compatible endpoint and is a
model experiment, not production validation.

## Fixture Contract

backend/app/agent/fixtures/agent_eval_v2_cases.json contains 33 strict Pydantic
scenarios: 30 agent_quality scenarios (8 multi-turn baselines, 6 ambiguity
controls, 4 confirmation/resume flows, 6 Job/result grounding flows, and 6
capability-isolation checks) plus 3 evaluator_self_test scenarios.
The self-tests deliberately use a wrong checksum, wrong numeric claim, or
invalid row id; they must be rejected by the verifier and are excluded from
Agent-quality aggregates.

Each scenario owns a fresh InMemorySaver, dataset references, fixture Jobs,
recorded-model sequence, trace id, and tool recorder. Trials cannot inherit
another case's checkpoint, file bytes, or Job list. Fixtures and reports are
bounded and never retain raw prompts, CSV bytes, credentials, DSNs,
object-storage locations, or tool arguments.

Confirmation fixtures use the pending plan placeholders. The runner reads
values from the checkpoint-owned interrupt and strips public kind/interrupt_id
before Command(resume=...), matching production. The fingerprint-change case
returns to clarification; the duplicate-resume case proves a repeated
idempotency key does not submit a second Job.

## Metrics and Gate

Reports include pass@1, multi-trial consistency, terminal success, clarification
precision/recall, citation validity, numeric consistency, unsupported-claim
rate, multi-turn context availability, latency, trace linkage, reported token
totals, and unknown-usage calls. Cost is explicitly unknown; no cloud price is
fabricated for local vLLM.

The release gate requires gated quality scenarios to reach expected terminal
states, zero illegal automatic executions, 1.0 clarification precision/recall,
1.0 citation/numeric consistency, zero unsupported claims, and successful
evaluator self-tests. Capability isolation is reported separately with pass@1,
tool parameter accuracy, trace linkage, and illegal auto-execution counts.

The eight multi-turn scenarios are non-gating baselines because durable
cross-runtime message history is Stage 3 work. They remain visible rather than
being presented as a current production guarantee.

## Commands

    .venv\Scripts\python.exe scripts/run_agent_eval_v2.py
    .venv\Scripts\python.exe scripts/run_agent_eval_v2.py --json
    .venv\Scripts\python.exe scripts/run_agent_eval_v2.py --trials 3

These commands are deterministic and do not contact a model endpoint. A live
run is explicit and defaults to three trials:

    .venv\Scripts\python.exe scripts/run_agent_eval_v2.py --live-model
      --base-url http://127.0.0.1:18000/v1 --model Qwen3-14B-AWQ

Do not run --live-model in normal CI. Supply credentials through the runtime
environment rather than shell history.

## Verification and Boundaries

backend/tests/test_agent_eval_v2.py covers schema validation, unique IDs,
required coverage, evaluator rejection, release-gate regression, deterministic
replay, isolation, trace/token recording, dynamic confirmation payloads,
fingerprint changes, and CLI isolation. The deterministic runner proves graph
integration, not actual vLLM quality or production latency. Wall-clock timeout,
cancellation, retry/DLQ behavior, real cost accounting, and durable multi-turn
history remain later stages.

Final local verification for this stage:

    .venv\Scripts\python.exe -m pytest backend/tests -q
    212 passed, 2 skipped

    .venv\Scripts\python.exe scripts/run_agent_eval_v2.py
    24 gated quality cases, capability 6 cases, evaluator self-test PASS
    release gate PASS

    npm --prefix frontend run test -- --run
    4 files passed, 13 tests passed

    npm --prefix frontend run build
    succeeded

The backend run uses a repository-local ignored temporary directory on Windows
because the host-wide pytest temporary root was not writable. The only build
warning is the existing large Plotly chunk; it does not fail the build.
