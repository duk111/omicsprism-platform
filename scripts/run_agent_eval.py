from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pydantic import BaseModel, ConfigDict, Field

from backend.app.agent.domain_eval import (
    evaluate_ambiguity_cases,
    evaluate_grounded_qa_cases,
    evaluate_parameter_inference,
    load_ambiguity_cases,
    load_grounded_qa_cases,
    load_parameter_inference_cases,
)


class ParameterInferenceSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_count: int = Field(ge=0)
    resolved_rate: float = Field(ge=0, le=1)
    field_accuracy: float = Field(ge=0, le=1)
    pair_accuracy: float = Field(ge=0, le=1)
    full_contrast_exact_match_rate: float = Field(ge=0, le=1)
    failing_case_ids: list[str] = Field(default_factory=list)


class AmbiguitySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_count: int = Field(ge=0)
    clarification_recall: float = Field(ge=0, le=1)
    false_positive_rate: float = Field(ge=0, le=1)
    illegal_auto_run_count: int = Field(ge=0)
    all_cases_matched: bool


class GroundedQASummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_count: int = Field(ge=0)
    citation_validity: float = Field(ge=0, le=1)
    numeric_consistency: float = Field(ge=0, le=1)
    unsupported_claim_rate: float = Field(ge=0, le=1)
    hallucinated_entity_count: int = Field(ge=0)


class CapabilityIsolationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool
    exit_code: int
    command: str
    detail: str = Field(default="", max_length=2000)


class AgentEvalReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parameter_inference: ParameterInferenceSummary
    ambiguity: AmbiguitySummary
    grounded_qa: GroundedQASummary
    capability_isolation: CapabilityIsolationSummary
    passed: bool


def run_domain_evaluation(*, include_capability: bool = True) -> AgentEvalReport:
    parameter_results = [
        evaluate_parameter_inference(case)
        for case in load_parameter_inference_cases()
    ]
    parameter = ParameterInferenceSummary(
        case_count=len(parameter_results),
        resolved_rate=_rate(
            sum(result.resolved for result in parameter_results), len(parameter_results)
        ),
        field_accuracy=_mean(
            result.field_accuracy for result in parameter_results
        ),
        pair_accuracy=_mean(
            result.pair_accuracy for result in parameter_results
        ),
        full_contrast_exact_match_rate=_rate(
            sum(result.full_contrast_exact_match for result in parameter_results),
            len(parameter_results),
        ),
        failing_case_ids=[
            result.case_id
            for result in parameter_results
            if not result.full_contrast_exact_match
        ],
    )

    ambiguity_eval = evaluate_ambiguity_cases(load_ambiguity_cases())
    ambiguity = AmbiguitySummary(
        case_count=ambiguity_eval.case_count,
        clarification_recall=ambiguity_eval.clarification_recall,
        false_positive_rate=ambiguity_eval.false_positive_rate,
        illegal_auto_run_count=ambiguity_eval.illegal_auto_run_count,
        all_cases_matched=all(case.matched for case in ambiguity_eval.cases),
    )

    grounded_eval = evaluate_grounded_qa_cases(load_grounded_qa_cases())
    grounded = GroundedQASummary(
        case_count=grounded_eval.case_count,
        citation_validity=grounded_eval.citation_validity,
        numeric_consistency=grounded_eval.numeric_consistency,
        unsupported_claim_rate=grounded_eval.unsupported_claim_rate,
        hallucinated_entity_count=grounded_eval.hallucinated_entity_count,
    )

    capability = (
        run_capability_isolation()
        if include_capability
        else CapabilityIsolationSummary(
            passed=True,
            exit_code=0,
            command="skipped",
            detail="capability isolation regression was skipped",
        )
    )
    passed = (
        parameter.full_contrast_exact_match_rate == 1
        and ambiguity.clarification_recall == 1
        and ambiguity.false_positive_rate == 0
        and ambiguity.illegal_auto_run_count == 0
        and ambiguity.all_cases_matched
        and capability.passed
    )
    return AgentEvalReport(
        parameter_inference=parameter,
        ambiguity=ambiguity,
        grounded_qa=grounded,
        capability_isolation=capability,
        passed=passed,
    )


def run_capability_isolation(
    *, repo_root: Path = REPO_ROOT,
) -> CapabilityIsolationSummary:
    command = [
        sys.executable,
        "-m",
        "pytest",
        "backend/tests/test_agent_eval_capability_isolation.py",
        "-q",
        "--disable-warnings",
        "--maxfail=1",
    ]
    command_text = " ".join(command)
    try:
        completed = subprocess.run(
            command,
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return CapabilityIsolationSummary(
            passed=False,
            exit_code=127,
            command=command_text,
            detail=str(exc),
        )
    detail = (completed.stdout + completed.stderr).strip()
    return CapabilityIsolationSummary(
        passed=completed.returncode == 0,
        exit_code=completed.returncode,
        command=command_text,
        detail=detail[-2000:],
    )


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 1.0


def _mean(values: object) -> float:
    numbers = [float(value) for value in values]  # type: ignore[union-attr]
    return sum(numbers) / len(numbers) if numbers else 1.0


def _print_report(report: AgentEvalReport) -> None:
    parameter = report.parameter_inference
    print("Parameter Inference")
    print(f"  cases: {parameter.case_count}")
    print(f"  resolved rate: {parameter.resolved_rate:.3f}")
    print(f"  field accuracy: {parameter.field_accuracy:.3f}")
    print(f"  pair accuracy: {parameter.pair_accuracy:.3f}")
    print(f"  full contrast exact match: {parameter.full_contrast_exact_match_rate:.3f}")

    ambiguity = report.ambiguity
    print("Ambiguity")
    print(f"  cases: {ambiguity.case_count}")
    print(f"  clarification recall: {ambiguity.clarification_recall:.3f}")
    print(f"  false-positive rate: {ambiguity.false_positive_rate:.3f}")
    print(f"  illegal auto-run count: {ambiguity.illegal_auto_run_count}")

    grounded = report.grounded_qa
    print("Grounded QA")
    print(f"  cases: {grounded.case_count}")
    print(f"  citation validity: {grounded.citation_validity:.3f}")
    print(f"  numeric consistency: {grounded.numeric_consistency:.3f}")
    print(f"  unsupported-claim rate: {grounded.unsupported_claim_rate:.3f}")
    print(f"  hallucinated entity count: {grounded.hallucinated_entity_count}")

    capability = report.capability_isolation
    print("Capability Isolation")
    print(f"  status: {'PASS' if capability.passed else 'FAIL'}")
    if capability.detail:
        print(f"  detail: {capability.detail.splitlines()[-1]}")
    print(f"Overall: {'PASS' if report.passed else 'FAIL'}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run deterministic OmicsPrism Agent domain evaluations")
    parser.add_argument(
        "--json",
        action="store_true",
        help="print the typed evaluation report as JSON",
    )
    parser.add_argument(
        "--skip-capability",
        action="store_true",
        help="skip the pytest capability isolation regression",
    )
    args = parser.parse_args(argv)
    report = run_domain_evaluation(include_capability=not args.skip_capability)
    if args.json:
        print(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2))
    else:
        _print_report(report)
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
