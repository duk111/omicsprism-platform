from __future__ import annotations

from backend.app.agent.domain_eval import (
    DEFAULT_PARAMETER_CASES_PATH,
    evaluate_parameter_inference,
    load_parameter_inference_cases,
)


def test_parameter_inference_fixture_covers_deg_dem_and_gma() -> None:
    cases = load_parameter_inference_cases(DEFAULT_PARAMETER_CASES_PATH)

    assert {case.expected.analysis_type for case in cases} == {"DEG", "DEM", "GMA"}
    assert len({case.case_id for case in cases}) == len(cases)


def test_parameter_inference_cases_match_expected_domain_contract() -> None:
    cases = load_parameter_inference_cases(DEFAULT_PARAMETER_CASES_PATH)
    results = [evaluate_parameter_inference(case) for case in cases]

    assert all(result.resolved for result in results)
    assert all(result.field_accuracy == 1 for result in results)
    assert all(result.pair_accuracy == 1 for result in results)
    assert all(result.full_contrast_exact_match for result in results)
    assert all(result.issues == [] for result in results)


def test_parameter_inference_reports_mismatch_without_changing_resolver() -> None:
    case = load_parameter_inference_cases(DEFAULT_PARAMETER_CASES_PATH)[0]
    altered = case.model_copy(update={
        "expected": case.expected.model_copy(update={"tested_level": "drought"}),
    })

    result = evaluate_parameter_inference(altered)

    assert result.pair_accuracy == 0.5
    assert not result.full_contrast_exact_match
    assert "tested_level" in result.issues[0]
