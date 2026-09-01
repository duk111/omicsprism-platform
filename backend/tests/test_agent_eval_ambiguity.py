from __future__ import annotations

from backend.app.agent.domain_eval import (
    DEFAULT_AMBIGUITY_CASES_PATH,
    evaluate_ambiguity_case,
    evaluate_ambiguity_cases,
    load_ambiguity_cases,
)


def test_ambiguity_fixture_covers_clarification_and_unique_resolution() -> None:
    cases = load_ambiguity_cases(DEFAULT_AMBIGUITY_CASES_PATH)

    assert {case.should_clarify for case in cases} == {True, False}
    assert len({case.case_id for case in cases}) == len(cases)


def test_ambiguity_metrics_have_no_illegal_auto_run() -> None:
    evaluation = evaluate_ambiguity_cases(load_ambiguity_cases())

    assert evaluation.case_count == 4
    assert evaluation.clarification_recall == 1
    assert evaluation.false_positive_rate == 0
    assert evaluation.illegal_auto_run_count == 0
    assert all(case.matched for case in evaluation.cases)


def test_ambiguity_case_reports_illegal_auto_run() -> None:
    case = load_ambiguity_cases()[2].model_copy(
        update={"should_clarify": True, "expected_missing_field": "contrast"}
    )
    result = evaluate_ambiguity_case(case)

    assert not result.clarification_requested
    assert result.auto_run
    assert result.illegal_auto_run
    assert not result.matched
