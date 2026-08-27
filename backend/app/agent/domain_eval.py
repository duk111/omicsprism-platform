from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .dataset_profile import DatasetProfile
from .graph import JobSummary
from .grounding import NO_EVIDENCE_TEXT, EvidenceGroundingError, EvidenceGrounder
from .param_resolver import AnalysisProposal, ScopeSpec, resolve_analysis_request
from .schemas import GroundedAnswer, ToolName, ToolResult
from .verifier import AnswerVerifier


AnalysisName = Literal["DEG", "DEM", "GMA"]
DEFAULT_PARAMETER_CASES_PATH = Path(__file__).with_name("fixtures") / "parameter_inference_cases.json"
DEFAULT_AMBIGUITY_CASES_PATH = Path(__file__).with_name("fixtures") / "ambiguity_cases.json"
DEFAULT_GROUNDED_QA_CASES_PATH = Path(__file__).with_name("fixtures") / "grounded_qa_cases.json"


class ExpectedContrast(BaseModel):
    model_config = ConfigDict(extra="forbid")

    analysis_type: AnalysisName
    compare_field: str | None = None
    tested_level: str | None = None
    reference_level: str | None = None
    scope: ScopeSpec = Field(default_factory=lambda: ScopeSpec(mode="all"))


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


class GroundedQACase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    job: JobSummary
    evidence: ToolResult
    draft: GroundedAnswer | None = None
    entity_field: str | None = None
    claimed_entities: list[str] = Field(default_factory=list, max_length=20)
    expected_evidence_sufficient: bool
    expected_refusal: bool


class GroundedQAResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    evidence_sufficient: bool
    citation_valid: bool
    numeric_consistent: bool
    unsupported_claim_rate: float = Field(ge=0, le=1)
    hallucinated_entity_count: int = Field(ge=0)
    refused_insufficient_evidence: bool
    matched: bool
    issues: list[str] = Field(default_factory=list)


class GroundedQAEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_count: int = Field(ge=0)
    citation_validity: float = Field(ge=0, le=1)
    numeric_consistency: float = Field(ge=0, le=1)
    unsupported_claim_rate: float = Field(ge=0, le=1)
    hallucinated_entity_count: int = Field(ge=0)
    cases: list[GroundedQAResult] = Field(default_factory=list)


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
    actual_scope = getattr(actual_contrast, "scope", None)
    scope_matches = actual_contrast is None or actual_scope == expected.scope
    full_match = (
        field_accuracy == 1
        and pair_accuracy == 1
        and scope_matches
        and (params is not None)
    )
    issues = [
        f"{name}: expected {wanted!r}, got {actual!r}"
        for name, actual, wanted in compared_fields + compared_pair
        if actual != wanted
    ]
    if actual_contrast is not None and actual_scope != expected.scope:
        issues.append(f"scope: expected {expected.scope!r}, got {actual_scope!r}")
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


def load_grounded_qa_cases(
    path: Path = DEFAULT_GROUNDED_QA_CASES_PATH,
) -> list[GroundedQACase]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("grounded QA case file must contain a JSON array")
    cases = [GroundedQACase.model_validate(item) for item in payload]
    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("grounded QA case ids must be unique")
    return cases


def evaluate_grounded_qa_case(case: GroundedQACase) -> GroundedQAResult:
    evidence = case.evidence
    issues: list[str] = []
    evidence_sufficient = bool(evidence.ok and evidence.rows)
    artifact_bound = evidence.artifact is not None and evidence.artifact in case.job.artifacts
    owner_bound = case.job.owner_id == case.user_id
    if not artifact_bound:
        issues.append("evidence artifact is not listed on the job")
    if not owner_bound:
        issues.append("job owner does not match the case user")
    if evidence.tool is not ToolName.QUERY_RESULT_EVIDENCE or not evidence.ok:
        issues.append("evidence is not a successful query_result response")

    answer: GroundedAnswer | None = None
    checks = []
    if evidence_sufficient and artifact_bound and owner_bound:
        try:
            answer = EvidenceGrounder().ground(evidence, case.draft)
            verdict = AnswerVerifier().verify(answer, [evidence])
            checks = verdict.checks
        except EvidenceGroundingError as exc:
            issues.append(str(exc))
    elif not evidence_sufficient:
        answer = EvidenceGrounder().ground(evidence)

    citation_valid = bool(checks) and all(check.citation_valid for check in checks)
    numeric_consistent = bool(checks) and all(check.number_matches_evidence for check in checks)
    entity_values = _evidence_entity_values(evidence, case.entity_field)
    hallucinated_entity_count = sum(
        entity not in entity_values for entity in case.claimed_entities
    )
    invalid_claim_count = sum(
        not (check.citation_valid and check.number_matches_evidence and not check.beyond_evidence)
        for check in checks
    )
    claim_count = len(answer.claims) if answer is not None else 0
    unsupported_claim_rate = min(
        1.0,
        (invalid_claim_count + hallucinated_entity_count) / max(1, claim_count),
    )
    refused_insufficient_evidence = (
        not evidence_sufficient
        and answer is not None
        and len(answer.claims) == 1
        and answer.claims[0].text == NO_EVIDENCE_TEXT
    )
    matched = (
        evidence_sufficient == case.expected_evidence_sufficient
        and refused_insufficient_evidence == case.expected_refusal
        and (not evidence_sufficient or (citation_valid and numeric_consistent))
        and hallucinated_entity_count == 0
    )
    if evidence_sufficient != case.expected_evidence_sufficient:
        issues.append("evidence sufficiency does not match the fixture expectation")
    if refused_insufficient_evidence != case.expected_refusal:
        issues.append("insufficient-evidence refusal does not match the fixture expectation")
    if hallucinated_entity_count:
        issues.append("claimed entity is absent from cited evidence rows")
    return GroundedQAResult(
        case_id=case.case_id,
        evidence_sufficient=evidence_sufficient,
        citation_valid=citation_valid,
        numeric_consistent=numeric_consistent,
        unsupported_claim_rate=unsupported_claim_rate,
        hallucinated_entity_count=hallucinated_entity_count,
        refused_insufficient_evidence=refused_insufficient_evidence,
        matched=matched,
        issues=issues,
    )


def evaluate_grounded_qa_cases(cases: list[GroundedQACase]) -> GroundedQAEvaluation:
    results = [evaluate_grounded_qa_case(case) for case in cases]
    return GroundedQAEvaluation(
        case_count=len(results),
        citation_validity=_rate(
            sum(result.citation_valid for result in results), len(results)
        ),
        numeric_consistency=_rate(
            sum(result.numeric_consistent for result in results), len(results)
        ),
        unsupported_claim_rate=_rate(
            sum(result.unsupported_claim_rate for result in results), len(results)
        ),
        hallucinated_entity_count=sum(
            result.hallucinated_entity_count for result in results
        ),
        cases=results,
    )


def _accuracy(checks: list[tuple[str, object, object]]) -> float:
    if not checks:
        return 1.0
    return sum(actual == expected for _, actual, expected in checks) / len(checks)


def _rate(numerator: float, denominator: int) -> float:
    return numerator / denominator if denominator else 1.0


def _evidence_entity_values(evidence: ToolResult, entity_field: str | None) -> set[str]:
    if not entity_field:
        return set()
    return {
        str(row[entity_field])
        for row in evidence.rows
        if entity_field in row and str(row[entity_field]).strip()
    }
