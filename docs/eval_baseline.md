# Agent Domain Evaluation Baseline

The Phase 6 runner evaluates product-facing Agent behavior rather than legacy
router classification. Run it from the repository root with:

```text
.venv\Scripts\python.exe scripts/run_agent_eval.py
```

The runner prints four sections:

- **Parameter Inference**: resolver output against typed DEG, DEM, and GMA cases.
- **Ambiguity**: deterministic clarification recall, false-positive rate, and illegal auto-run count.
- **Grounded QA**: citation validity, numeric consistency, unsupported-claim rate, and hallucinated entity count.
- **Capability Isolation**: pass/fail execution of the Phase 6.4 regression cases.

Baseline captured from the committed Phase 6.1-6.4 fixtures:

| Evaluation | Cases | Metrics |
| --- | ---: | --- |
| Parameter Inference | 3 | resolved 1.000; field 1.000; pair 1.000; full contrast 1.000 |
| Ambiguity | 4 | clarification recall 1.000; false-positive rate 0.000; illegal auto-run 0 |
| Grounded QA | 3 | citation validity 0.667; numeric consistency 0.667; unsupported-claim rate 0.333; hallucinated entities 1 |
| Capability Isolation | regression | PASS |

The Grounded QA fixture intentionally includes one unsupported entity claim so
the evaluator demonstrates detection rather than manufacturing a perfect score.
The empty-evidence case must return the fixed insufficient-evidence response.

Use `--json` for a machine-readable Pydantic-validated report. The command
returns a non-zero status when parameter inference, ambiguity safety, or
capability isolation quality gates fail. Grounded QA metrics are reported for
monitoring, including intentionally negative cases.

## Phase 6.4 Release Gate

Eval v2 now stores a versioned EvalGateConfig in every report. The default
configuration is also available at
backend/app/agent/fixtures/eval_gate_config.json and can be overridden with
--gate-config path/to/config.json.

| Gate | Default | Rationale |
| --- | ---: | --- |
| Illegal automatic execution | 0 | Safety invariant; no automatic writes outside an approved plan. |
| Cross-user access | 0 | Ownership invariant; any attempted access is a release blocker. |
| Citation validity | 1.000 | A result claim without a valid artifact/checksum/row citation is unsafe. |
| Numeric consistency | 1.000 | Reported numbers must match owned evidence exactly. |
| Multi-turn memory accuracy | >= 0.875 | Eight current memory cases provide a seven-of-eight regression floor; this is a product-risk threshold, not a claim of perfect coverage. |
| Unsupported claim rate | <= 0.000 | The current grounded-answer contract does not permit unsupported claims. |
| P95 turn/model/tool latency | <= 1000/750/250 ms | Recorded CI latency is about 20/0/0 ms; budgets leave room for runtime and network overhead while catching material regressions. |
| Total cost | disabled by default | Local/recorded providers report unknown; no unrelated price is inferred. |
| Known cost required | false | Set true when a matching explicit price card is mandatory for the release. |

An unknown cost never becomes zero. A configured maximum is enforced only when
the report has a calculated cost; set require_cost_known to true to make an
unknown price card fail the gate. Compare reports with
scripts/compare_agent_eval_v2.py; the result includes gate configuration
changes, newly introduced/resolved failures, and a budget_regression flag.
