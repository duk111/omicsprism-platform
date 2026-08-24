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
