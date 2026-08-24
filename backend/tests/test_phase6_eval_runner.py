from __future__ import annotations

import json

from scripts.run_agent_eval import main, run_domain_evaluation


def test_eval_runner_reports_all_domain_sections_without_subprocess() -> None:
    report = run_domain_evaluation(include_capability=False)

    assert report.passed
    assert report.parameter_inference.case_count == 3
    assert report.ambiguity.case_count == 4
    assert report.grounded_qa.case_count == 3
    assert report.capability_isolation.command == "skipped"


def test_eval_runner_json_output_contains_three_domain_groups(capsys) -> None:
    assert main(["--json", "--skip-capability"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {
        "parameter_inference",
        "ambiguity",
        "grounded_qa",
        "capability_isolation",
        "passed",
    }
    assert payload["parameter_inference"]["full_contrast_exact_match_rate"] == 1
    assert payload["ambiguity"]["illegal_auto_run_count"] == 0
    assert payload["grounded_qa"]["hallucinated_entity_count"] == 1
