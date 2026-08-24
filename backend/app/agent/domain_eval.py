from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .dataset_profile import DatasetProfile
from .param_resolver import AnalysisProposal, resolve_analysis_request


AnalysisName = Literal["DEG", "DEM", "GMA"]
DEFAULT_PARAMETER_CASES_PATH = Path(__file__).with_name("fixtures") / "parameter_inference_cases.json"
DEFAULT_AMBIGUITY_CASES_PATH = Path(__file__).with_name("fixtures") / "ambiguity_cases.json"


class ExpectedContrast(BaseModel):
    model_config = ConfigDict(extra="forbid")

    analysis_type: AnalysisName
    compare_field: str | None = None
    tested_level: str | None = None
    reference_level: str | None = None
    same_fields: dict[str, str] = Field(default_factory=dict)


class ParameterInferenceCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1)
    user_message: str
    profiles: list[DatasetProfile]
    proposal: AnalysisProposal
    expected: ExpectedContrast


class ParameterInferenceResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    resolved: bool
    field_accuracy: float = Field(ge=0, le=1)
    pair_accuracy: float = Field(ge=0, le=1)
    full_contrast_exact_match: bool
    issues: list[str] = Field(default_factory=list)


class AmbiguityCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1)
    user_message: str
    profiles: list[DatasetProfile]
    proposal: AnalysisProposal
    should_clarify: bool
    expected_missing_field: str | None = None


class AmbiguityCaseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    should_clarify: bool
    clarification_requested: bool
    auto_run: bool
    illegal_auto_run: bool
    matched: bool
    missing_field: str | None = None
    issues: list[str] = Field(default_factory=list)


class AmbiguityEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_count: int = Field(ge=0)
    clarification_case_count: int = Field(ge=0)
    no_clarification_case_count: int = Field(ge=0)
    clarification_recall: float = Field(ge=0, le=1)
    false_positive_rate: float = Field(ge=0, le=1)
    illegal_auto_run_count: int = Field(ge=0)
    cases: list[AmbiguityCaseResult] = Field(default_factory=list)


def load_parameter_inference_cases(
    path: Path = DEFAULT_PARAMETER_CASES_PATH,
) -> list[ParameterInferenceCase]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("parameter inference case file must contain a JSON array")
    cases = [ParameterInferenceCase.model_validate(item) for item in payload]
    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("parameter inference case ids must be unique")
    return cases


def evaluate_parameter_inference(case: ParameterInferenceCase) -> ParameterInferenceResult:
    resolved = resolve_analysis_request(
        case.user_message,
        case.profiles,
        case.proposal,
    )
    expected = case.expected
    params = resolved.params
    actual_type = resolved.analysis_type
    actual_contrast = getattr(params, "contrast", None)
    field_checks = [
        ("analysis_type", actual_type, expected.analysis_type),
        ("compare_field", getattr(actual_contrast, "compare_field", None), expected.compare_field),
    ]
    compared_fields = [(name, actual, wanted) for name, actual, wanted in field_checks if wanted is not None]
    field_accuracy = _accuracy(compared_fields)
    pair_checks = [
        ("tested_level", getattr(actual_contrast, "tested_level", None), expected.tested_level),
        ("reference_level", getattr(actual_contrast, "reference_level", None), expected.reference_level),
    ]
    compared_pair = [(name, actual, wanted) for name, actual, wanted in pair_checks if wanted is not None]
    pair_accuracy = _accuracy(compared_pair)
    actual_same = getattr(actual_contrast, "same_fields", {})
    full_match = (
        field_accuracy == 1
        and pair_accuracy == 1
        and actual_same == expected.same_fields
        and (params is not None)
    )
    issues = [
        f"{name}: expected {wanted!r}, got {actual!r}"
        for name, actual, wanted in compared_fields + compared_pair
        if actual != wanted
    ]
    if actual_same != expected.same_fields:
        issues.append(f"same_fields: expected {expected.same_fields!r}, got {actual_same!r}")
    if not resolved.missing and params is None:
        issues.append("resolver returned no parameters")
    return ParameterInferenceResult(
        case_id=case.case_id,
        resolved=params is not None,
        field_accuracy=field_accuracy,
        pair_accuracy=pair_accuracy,
        full_contrast_exact_match=full_match,
        issues=issues,
    )


def load_ambiguity_cases(
    path: Path = DEFAULT_AMBIGUITY_CASES_PATH,
) -> list[AmbiguityCase]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("ambiguity case file must contain a JSON array")
    cases = [AmbiguityCase.model_validate(item) for item in payload]
    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("ambiguity case ids must be unique")
    return cases


def evaluate_ambiguity_case(case: AmbiguityCase) -> AmbiguityCaseResult:
    resolved = resolve_analysis_request(case.user_message, case.profiles, case.proposal)
    clarification_requested = bool(resolved.missing)
    auto_run = resolved.params is not None and not resolved.missing
    missing_field = resolved.missing[0].field if resolved.missing else None
    illegal_auto_run = case.should_clarify and auto_run
    matched = clarification_requested == case.should_clarify and (
        case.expected_missing_field is None
        or missing_field == case.expected_missing_field
    )
    issues: list[str] = []
    if clarification_requested != case.should_clarify:
        expected = "clarification" if case.should_clarify else "resolved execution"
        actual = "clarification" if clarification_requested else "resolved execution"
        issues.append(f"expected {expected}, got {actual}")
    if case.expected_missing_field is not None and missing_field != case.expected_missing_field:
        issues.append(
            f"missing field: expected {case.expected_missing_field!r}, got {missing_field!r}"
        )
    if illegal_auto_run:
        issues.append("ambiguous case resolved to executable parameters")
    return AmbiguityCaseResult(
        case_id=case.case_id,
        should_clarify=case.should_clarify,
        clarification_requested=clarification_requested,
        auto_run=auto_run,
        illegal_auto_run=illegal_auto_run,
        matched=matched,
        missing_field=missing_field,
        issues=issues,
    )


def evaluate_ambiguity_cases(cases: list[AmbiguityCase]) -> AmbiguityEvaluation:
    results = [evaluate_ambiguity_case(case) for case in cases]
    clarification_cases = [result for result in results if result.should_clarify]
    no_clarification_cases = [result for result in results if not result.should_clarify]
    true_positives = sum(
        result.clarification_requested for result in clarification_cases
    )
    false_positives = sum(
        result.clarification_requested for result in no_clarification_cases
    )
    return AmbiguityEvaluation(
        case_count=len(results),
        clarification_case_count=len(clarification_cases),
        no_clarification_case_count=len(no_clarification_cases),
        clarification_recall=_rate(true_positives, len(clarification_cases)),
        false_positive_rate=_rate(false_positives, len(no_clarification_cases)),
        illegal_auto_run_count=sum(result.illegal_auto_run for result in results),
        cases=results,
    )


def _accuracy(checks: list[tuple[str, object, object]]) -> float:
    if not checks:
        return 1.0
    return sum(actual == expected for _, actual, expected in checks) / len(checks)


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 1.0
