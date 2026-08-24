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
| 6.4 | `25 passed` | `160 passed, 2 skipped, 3 failed` | Capability isolation regressions. |
| 6.5 | `15 passed` | `162 passed, 2 skipped, 3 failed` | Eval runner tests and domain evaluation runner. |
| Schema cleanup | `54 passed` | `150 passed, 2 skipped, 3 failed` | Removed legacy decision/context contracts; the three known bundle inheritance failures remain. |
| State/lease cleanup | `34 passed` | `150 passed, 2 skipped, 3 failed` | Removed zombie run-state fields and turn leases; the same three known bundle inheritance failures remain. |

| Frontend unit tests | `4 files, 13 passed` | Not applicable | `npm --prefix frontend run test`. |
| Frontend production build | Successful | Not applicable | `npm --prefix frontend run build`; Vite emitted a large-chunk warning only. |

The Phase 6 targeted suites were run with the relevant prior suites. For
example, 6.3 included the Phase 4 grounding and JSON query tests, and 6.4
included the complete graph flow suite.

## Known Full-Test Failures

The same three failures remained throughout the Phase 6 full backend runs:

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

These failures are in the pre-existing in-memory product store bundle
inheritance path. They were not changed as part of Phase 6 because the Phase 6
scope is domain evaluation and the product store is outside that task. No test
was skipped, weakened, or altered to hide the failures.

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
