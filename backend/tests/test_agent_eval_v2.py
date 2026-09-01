from __future__ import annotations

import json

import pytest

from backend.app.agent.eval_v2 import (
    AgentEvalV2Case,
    DEFAULT_EVAL_V2_CASES_PATH,
    EvalExpectation,
    load_agent_eval_v2_cases,
    run_ci_agent_evaluation,
)
from scripts.run_agent_eval_v2 import main


def _quality_cases() -> list[AgentEvalV2Case]:
    return [case for case in load_agent_eval_v2_cases() if case.suite == "agent_quality"]


def test_fixture_schema_ids_and_required_phase_two_coverage() -> None:
    cases = load_agent_eval_v2_cases(DEFAULT_EVAL_V2_CASES_PATH)
    quality = [case for case in cases if case.suite == "agent_quality"]
    counts = {
        category: sum(case.category == category for case in quality)
        for category in {
            "multi_turn_memory",
            "ambiguity",
            "confirmation",
            "result_grounding",
            "capability_isolation",
        }
    }

    assert len(cases) == 33
    assert len(quality) == 30
    assert len({case.case_id for case in cases}) == len(cases)
    assert counts == {
        "multi_turn_memory": 8,
        "ambiguity": 6,
        "confirmation": 4,
        "result_grounding": 6,
        "capability_isolation": 6,
    }
    assert sum(case.suite == "evaluator_self_test" for case in cases) == 3


def test_case_schema_rejects_duplicate_transport_data_and_invalid_non_gating_case() -> None:
    payload = _quality_cases()[0].model_dump(mode="json")
    payload["turns"][0]["unexpected"] = True
    with pytest.raises(ValueError, match="unexpected"):
        AgentEvalV2Case.model_validate(payload)

    with pytest.raises(ValueError, match="non-gating"):
        EvalExpectation(terminal="completed", expected_job_count=0, release_gate=False)


def test_ci_recordings_are_reproducible_and_trials_are_environment_isolated() -> None:
    cases = [
        next(case for case in _quality_cases() if case.case_id == "confirmation-approve-001"),
        next(case for case in _quality_cases() if case.case_id == "result-query-gene-001"),
    ]
    self_tests = [
        case
        for case in load_agent_eval_v2_cases()
        if case.suite == "evaluator_self_test"
    ]
    first = run_ci_agent_evaluation(cases=[*cases, *self_tests], trials_per_case=2)
    second = run_ci_agent_evaluation(cases=[*cases, *self_tests], trials_per_case=2)

    assert first.release_gate.passed
    assert second.release_gate.passed
    assert [
        (case.case_id, [trial.model_dump(exclude={"latency_ms"}) for trial in case.trials])
        for case in first.cases
    ] == [
        (case.case_id, [trial.model_dump(exclude={"latency_ms"}) for trial in case.trials])
        for case in second.cases
    ]
    quality_cases = [case for case in first.cases if case.suite == "agent_quality"]
    assert all(case.consistency == 1 for case in first.cases)
    assert all(case.trials[0].trace_event_count > 0 for case in quality_cases)
    assert all(case.trials[0].reported_total_tokens is not None for case in quality_cases)


def test_confirmation_uses_checkpoint_plan_values_and_detects_fingerprint_change() -> None:
    cases = _quality_cases()
    approve = next(case for case in cases if case.case_id == "confirmation-approve-001")
    changed = next(case for case in cases if case.case_id == "confirmation-fingerprint-change-004")
    report = run_ci_agent_evaluation(cases=[approve, changed])
    approve_trial, changed_trial = (case.trials[0] for case in report.cases)

    assert approve_trial.matched and approve_trial.job_count == 1
    assert changed_trial.matched
    assert changed_trial.terminal == "interrupt"
    assert changed_trial.interrupt_kind == "clarification"
    assert changed_trial.job_count == 0


def test_evaluator_self_tests_keep_malformed_evidence_out_of_agent_quality_metrics() -> None:
    report = run_ci_agent_evaluation()

    assert report.evaluator_self_test_passed
    assert report.release_gate.passed
    assert report.quality.case_count == 24
    assert report.quality.known_gap_count == 8
    assert report.capability.case_count == 6
    assert report.capability.pass_at_1 == 1
    assert report.capability.tool_parameter_accuracy == 1
    assert report.capability.illegal_auto_execution_count == 0
    assert report.quality.citation_validity == 1
    assert report.quality.numeric_consistency == 1
    assert report.quality.unsupported_claim_rate == 0
    assert all(
        case.status == "passed"
        for case in report.cases
        if case.suite == "evaluator_self_test"
    )


def test_release_gate_fails_when_a_gated_grounded_case_regresses() -> None:
    case = next(case for case in _quality_cases() if case.case_id == "result-query-gene-001")
    broken = case.model_copy(update={
        "expected": case.expected.model_copy(update={"expects_grounded_answer": False}),
    })
    report = run_ci_agent_evaluation(cases=[broken])

    assert not report.release_gate.passed
    assert "release scenario terminal success" in report.release_gate.failures[0]


def test_cli_json_defaults_to_recorded_ci_runner_without_external_model(capsys) -> None:
    assert main(["--json", "--trials", "2"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["runner"] == "ci"
    assert payload["trials_per_case"] == 2
    assert payload["release_gate"]["passed"] is True
    assert payload["quality"]["unknown_usage_model_calls"] == 0


def test_live_runner_requires_explicit_endpoint_and_model() -> None:
    with pytest.raises(SystemExit):
        main(["--live-model"])
