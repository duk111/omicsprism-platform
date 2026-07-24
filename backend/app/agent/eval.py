from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from statistics import quantiles
from time import perf_counter
from typing import Mapping, Sequence
from uuid import uuid4

from fastapi import HTTPException

from ..errors import analysis_failure_detail
from ..job_store import JobStorageService, PostgresJobRepository
from ..models import AnalysisType, JobRecord, JobStatus
from ..preflight import build_contrast_preview
from .approvals import InMemoryApprovalGate
from .context import build_analysis_capabilities
from .grounding import EvidenceGrounder, EvidenceGroundingError, NO_EVIDENCE_TEXT
from .model import ModelAdapter, ModelBoundaryError, ScriptedModelAdapter, VllmModelAdapter
from .plans import InMemoryPlanStore, compute_plan_hash
from .router import RuleRouter
from .schemas import (
    ActiveProfile,
    AgentAction,
    AgentDecision,
    AgentState,
    Citation,
    EvalAssemblyName,
    EvalCaseResult,
    EvalCaseStatus,
    EvalCategory,
    EvalDiffReport,
    EvalRunReport,
    EvalSummary,
    Feasibility,
    FeasibilityVerdict,
    GoldenCase,
    GroundedAnswer,
    GroundedClaim,
    ModelContext,
    PlanRecord,
    RunFocus,
    RunState,
    RunStatus,
    ToolName,
    ToolResult,
)
from .tools import AgentInputFile, AgentToolRuntime
from .verifier import AnswerVerifier


DEFAULT_CASES_PATH = Path(__file__).with_name("eval_cases.json")


@dataclass(frozen=True)
class EvalAssembly:
    name: EvalAssemblyName
    label: str
    available: bool
    skip_reason: str | None
    model: ModelAdapter | None
    fixture_tools: bool
    in_memory_state: bool
    database_url: str | None = None
    cross_user_job_id: str | None = None
    cross_user_id: str | None = None


class EvalAssemblyFactory:
    def __init__(self, env: Mapping[str, str] | None = None) -> None:
        self.env = dict(os.environ if env is None else env)

    @staticmethod
    def unit(label: str = "stub-fixture") -> EvalAssembly:
        return EvalAssembly(
            name=EvalAssemblyName.UNIT,
            label=label,
            available=True,
            skip_reason=None,
            model=None,
            fixture_tools=True,
            in_memory_state=True,
        )

    def offline(self) -> EvalAssembly:
        return self._live(EvalAssemblyName.OFFLINE, require_database=False)

    def production(self) -> EvalAssembly:
        return self._live(EvalAssemblyName.PRODUCTION, require_database=True)

    def _live(self, name: EvalAssemblyName, *, require_database: bool) -> EvalAssembly:
        model_url = self.env.get("OMICS_PRISM_AGENT_MODEL_URL", "").strip()
        model_name = self.env.get("OMICS_PRISM_AGENT_MODEL_NAME", "").strip()
        database_url = self.env.get("OMICS_PRISM_RUNTIME_DATABASE_URL", "").strip()
        cross_user_job_id = self.env.get("OMICS_PRISM_EVAL_CROSS_USER_JOB_ID", "").strip()
        cross_user_id = self.env.get("OMICS_PRISM_EVAL_CROSS_USER_ID", "").strip()
        missing = []
        if not model_url:
            missing.append("OMICS_PRISM_AGENT_MODEL_URL")
        if not model_name:
            missing.append("OMICS_PRISM_AGENT_MODEL_NAME")
        if require_database and not database_url:
            missing.append("OMICS_PRISM_RUNTIME_DATABASE_URL")
        if require_database and not cross_user_job_id:
            missing.append("OMICS_PRISM_EVAL_CROSS_USER_JOB_ID")
        if require_database and not cross_user_id:
            missing.append("OMICS_PRISM_EVAL_CROSS_USER_ID")
        reason = "missing required configuration: " + ", ".join(missing) if missing else None
        model = None if missing else VllmModelAdapter(
            base_url=model_url,
            model=model_name,
            api_key=self.env.get("OMICS_PRISM_AGENT_MODEL_API_KEY") or None,
        )
        return EvalAssembly(
            name=name,
            label=model_name or "unconfigured-live-model",
            available=not missing,
            skip_reason=reason,
            model=model,
            fixture_tools=name is EvalAssemblyName.OFFLINE,
            in_memory_state=name is EvalAssemblyName.OFFLINE,
            database_url=database_url or None,
            cross_user_job_id=cross_user_job_id or None,
            cross_user_id=cross_user_id or None,
        )


def load_golden_cases(path: Path = DEFAULT_CASES_PATH) -> list[GoldenCase]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("golden case file must contain a JSON array")
    cases = [GoldenCase.model_validate(item) for item in payload]
    identifiers = [case.case_id for case in cases]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("golden case ids must be unique")
    return cases


class EvalRunner:
    def run(self, cases: Sequence[GoldenCase], assembly: EvalAssembly) -> EvalRunReport:
        if not assembly.available:
            results = [self._skipped(case, assembly.skip_reason or "assembly unavailable") for case in cases]
        else:
            results = [self._run_case(case, assembly) for case in cases]
        return EvalRunReport(
            run_id=str(uuid4()),
            assembly=assembly.name,
            model_label=assembly.label,
            generated_at=datetime.now(timezone.utc),
            skip_reason=assembly.skip_reason,
            case_results=results,
            summary=_summarize(results),
        )

    def _run_case(self, case: GoldenCase, assembly: EvalAssembly) -> EvalCaseResult:
        started = perf_counter()
        model_calls = 1 if case.category is EvalCategory.RECOMMENDATION else 0
        schema_valid = True if case.category is EvalCategory.RECOMMENDATION else None
        try:
            issues = self._evaluate(case, assembly)
            status = EvalCaseStatus.PASSED if not issues else EvalCaseStatus.FAILED
        except ModelBoundaryError as exc:
            status = EvalCaseStatus.FAILED
            schema_valid = False
            issues = [f"{type(exc).__name__}: {exc}"]
        except Exception as exc:
            status = EvalCaseStatus.FAILED
            schema_valid = None
            issues = [f"{type(exc).__name__}: {exc}"]
        return EvalCaseResult(
            case_id=case.case_id,
            category=case.category,
            adversarial=case.adversarial,
            status=status,
            duration_ms=round((perf_counter() - started) * 1000, 3),
            model_calls=model_calls,
            schema_valid=schema_valid,
            issues=issues,
        )

    def _evaluate(self, case: GoldenCase, assembly: EvalAssembly) -> list[str]:
        if case.category is EvalCategory.ROUTER:
            return _eval_router(case)
        if case.category is EvalCategory.RECOMMENDATION:
            return _eval_recommendation(case, assembly)
        if case.category is EvalCategory.CONTRAST:
            return _eval_contrast(case)
        if case.category is EvalCategory.FAILURE:
            return _eval_failure(case)
        return _eval_grounding(case, assembly)

    @staticmethod
    def _skipped(case: GoldenCase, reason: str) -> EvalCaseResult:
        return EvalCaseResult(
            case_id=case.case_id,
            category=case.category,
            adversarial=case.adversarial,
            status=EvalCaseStatus.SKIPPED,
            duration_ms=0,
            model_calls=0,
            schema_valid=None,
            issues=[reason],
        )


def compare_reports(baseline: EvalRunReport, candidate: EvalRunReport) -> EvalDiffReport:
    base = {result.case_id: result for result in baseline.case_results}
    current = {result.case_id: result for result in candidate.case_results}
    common = sorted(set(base) & set(current))
    newly_failed = [case_id for case_id in common if base[case_id].status is EvalCaseStatus.PASSED and current[case_id].status is EvalCaseStatus.FAILED]
    newly_passed = [case_id for case_id in common if base[case_id].status is not EvalCaseStatus.PASSED and current[case_id].status is EvalCaseStatus.PASSED]
    newly_skipped = [case_id for case_id in common if base[case_id].status is not EvalCaseStatus.SKIPPED and current[case_id].status is EvalCaseStatus.SKIPPED]
    metric_names = sorted(set(baseline.summary.metrics) | set(candidate.summary.metrics))
    return EvalDiffReport(
        baseline_run_id=baseline.run_id,
        candidate_run_id=candidate.run_id,
        pass_rate_delta=_pass_rate(candidate.case_results) - _pass_rate(baseline.case_results),
        model_calls_delta=sum(item.model_calls for item in candidate.case_results) - sum(item.model_calls for item in baseline.case_results),
        p95_latency_ms_delta=_p95(candidate.case_results) - _p95(baseline.case_results),
        newly_failed=newly_failed,
        newly_passed=newly_passed,
        newly_skipped=newly_skipped,
        metric_deltas={
            name: _metric_delta(baseline.summary.metrics.get(name), candidate.summary.metrics.get(name))
            for name in metric_names
        },
    )


def _eval_router(case: GoldenCase) -> list[str]:
    state = _state(case.input.get("focus_job_ids", []))
    route = RuleRouter().route(str(case.input["message"]), state)
    return _mismatches({"intent": route.intent.value, "target_profile": route.target_profile.value}, case.expected)


def _eval_recommendation(case: GoldenCase, assembly: EvalAssembly) -> list[str]:
    if assembly.name is EvalAssemblyName.UNIT:
        recommended_types = _stub_recommendations(case.input)
        model: ModelAdapter = ScriptedModelAdapter([AgentDecision(
            action=AgentAction.PROPOSE_PLAN,
            reasoning_summary="golden fixture recommendation",
            feasibility=Feasibility(
                verdict=FeasibilityVerdict.ANSWERABLE if recommended_types else FeasibilityVerdict.NOT_ANSWERABLE,
                reasons=["golden fixture"],
                missing_information=[] if recommended_types else ["required inputs"],
            ),
            analysis_recommendations=recommended_types,
            requires_approval=bool(recommended_types),
            requested_params={},
        )])
    else:
        if assembly.model is None:
            return ["live model is unavailable"]
        model = assembly.model
    context = ModelContext(
        user_message=str(case.input["message"]),
        active_profile=ActiveProfile.ANALYSIS,
        state=AgentState.CHECK_INPUTS,
        in_scope_job_ids=[],
        conversation_summary=None,
        available_input_roles=list(case.input.get("available_inputs", [])),
        analysis_capabilities=build_analysis_capabilities(),
        available_tools=[ToolName.GET_ANALYSIS_SPEC, ToolName.INSPECT_UPLOADED_INPUTS, ToolName.RUN_PREFLIGHT],
    )
    decision = model.decide(context)
    actual = [item.value for item in decision.analysis_recommendations]
    return _mismatches({"analysis_recommendations": actual}, case.expected)


def _stub_recommendations(payload: Mapping[str, object]) -> list[AnalysisType]:
    available = {str(item).lower() for item in payload.get("available_inputs", [])}
    recommendations: list[AnalysisType] = []
    if {"counts", "metadata"}.issubset(available):
        recommendations.append(AnalysisType.DIFFERENTIAL)
    if {"metabs", "metadata"}.issubset(available):
        recommendations.append(AnalysisType.DEM)
    if {"transcriptome", "metabolome", "group"}.issubset(available):
        recommendations.append(AnalysisType.CORRELATION)
    return recommendations


def _eval_contrast(case: GoldenCase) -> list[str]:
    if case.input.get("mode") == "unapproved_submit":
        return _eval_unapproved_submit(case)
    contrasts, issues = build_contrast_preview(case.input["metadata"], case.input["params"])
    actual = {
        "can_submit": bool(contrasts),
        "contrast_count": len(contrasts),
        "issue_codes": sorted({issue.code.value for issue in issues}),
    }
    return _mismatches(actual, case.expected)


def _eval_unapproved_submit(case: GoldenCase) -> list[str]:
    plans = InMemoryPlanStore()
    jobs = _CountingJobStore()
    runtime = AgentToolRuntime(
        user_id="eval-user",
        inputs={
            "counts": AgentInputFile("counts.csv", b"gene,s1,s2,s3,s4\ng1,10,12,30,32\n"),
            "metadata": AgentInputFile(
                "metadata.csv",
                b"sample_id,treatment\ns1,control\ns2,control\ns3,salt\ns4,salt\n",
            ),
        },
        input_source_job_id="source-1",
        plans=plans,
        job_store=jobs,
        files=_UnusedFiles(),
        executor=_CountingExecutor(),
        approval_gate=InMemoryApprovalGate(),
    )
    params = {
        "compare_field": "treatment", "tested_levels": "salt",
        "reference_level": "control", "min_replicates": 2,
    }
    preflight = runtime.run_preflight(AnalysisType.DIFFERENTIAL, params)
    plan = PlanRecord(
        plan_id="plan-1", run_id="run-1", user_id="eval-user",
        analysis_type=AnalysisType.DIFFERENTIAL, source_job_id="source-1",
        requested_params=params, effective_params=preflight.rows[0]["effective_params"],
        contrasts=preflight.rows[0]["contrasts"], plan_hash="pending", approval_id=None,
    )
    plan.plan_hash = compute_plan_hash(plan)
    plans.save(plan)
    result = runtime.submit_approved_plan(plan.plan_id, "eval-key")
    return _mismatches({
        "error_code": result.error_code,
        "job_creations": len(jobs.saved),
    }, case.expected)


def _eval_failure(case: GoldenCase) -> list[str]:
    detail = analysis_failure_detail(str(case.input["error"]))
    expected_fragment = str(case.expected["suggestion_contains"]).lower()
    actual = {
        "code": detail.code,
        "suggestion_contains": any(expected_fragment in suggestion.lower() for suggestion in detail.suggestions),
    }
    return _mismatches(actual, {"code": case.expected["code"], "suggestion_contains": True})


def _eval_grounding(case: GoldenCase, assembly: EvalAssembly) -> list[str]:
    mode = str(case.input["mode"])
    if mode == "cross_user":
        return _eval_cross_user(case, assembly)
    evidence = _tool_result(case.input)
    grounder = EvidenceGrounder()
    if mode == "empty":
        answer = grounder.ground(evidence)
        return _mismatches({"text": answer.claims[0].text}, case.expected)
    if mode == "cell_injection":
        answer = grounder.ground(evidence)
        return _mismatches({
            "claim_count": len(answer.claims),
            "tool_calls": 0,
        }, case.expected)
    draft = GroundedAnswer(claims=[GroundedClaim(
        text=str(case.input["claim"]),
        citation=Citation(
            artifact=evidence.artifact or "",
            checksum=evidence.checksum or "",
            row_ids=list(case.input.get("row_ids", [1])),
        ),
    )])
    if mode == "union_direction":
        try:
            grounder.ground(evidence, draft)
        except EvidenceGroundingError:
            return _mismatches({"grounding_error": True}, case.expected)
        return ["union direction claim was accepted"]
    verdict = AnswerVerifier().verify(grounder.ground(evidence, draft), [evidence])
    return _mismatches({"verdict": verdict.verdict.value}, case.expected)


def _eval_cross_user(case: GoldenCase, assembly: EvalAssembly) -> list[str]:
    if assembly.name is EvalAssemblyName.PRODUCTION:
        if not all((assembly.database_url, assembly.cross_user_job_id, assembly.cross_user_id)):
            return ["production cross-user fixture is not configured"]
        job_store = JobStorageService(PostgresJobRepository(assembly.database_url or ""))
        job_id = assembly.cross_user_job_id or ""
        user_id = assembly.cross_user_id or ""
    else:
        now = datetime.now(timezone.utc)
        job = JobRecord(
            id="job-owned", project_name="fixture", analysis_type=AnalysisType.CORRELATION,
            status=JobStatus.SUCCEEDED, created_at=now, updated_at=now, owner_id="user-a",
        )
        job_store = _OwnedJobStore(job)
        job_id = "job-owned"
        user_id = "user-b"
    runtime = AgentToolRuntime(
        user_id=user_id, inputs={}, job_store=job_store, files=_UnusedFiles(),
    )
    try:
        runtime.query_result_evidence(job_id, "T02_High_Confidence_Network.csv")
    except HTTPException as exc:
        return _mismatches({"status_code": exc.status_code}, case.expected)
    return ["cross-user evidence access succeeded"]


class _OwnedJobStore:
    def __init__(self, job: JobRecord) -> None:
        self.job = job

    def get_for_user(self, job_id: str, user_id: str) -> JobRecord:
        if job_id != self.job.id or user_id != self.job.owner_id:
            raise HTTPException(status_code=404, detail="Job not found")
        return self.job


class _UnusedFiles:
    def read_artifact_text(self, job_id: str, relative_path: str) -> str:
        raise AssertionError("file access must not occur before ownership validation")


class _CountingJobStore:
    def __init__(self) -> None:
        self.saved: list[JobRecord] = []

    def save(self, job: JobRecord) -> None:
        self.saved.append(job)

    def get_for_user(self, job_id: str, user_id: str) -> JobRecord:
        raise HTTPException(status_code=404, detail="Job not found")


class _CountingExecutor:
    def __init__(self) -> None:
        self.enqueued: list[str] = []

    def enqueue(self, job_id: str) -> None:
        self.enqueued.append(job_id)


def _tool_result(payload: Mapping[str, object]) -> ToolResult:
    rows = list(payload.get("rows", []))
    return ToolResult(
        tool=ToolName.QUERY_RESULT_EVIDENCE,
        ok=True,
        rows=rows,
        truncated=False,
        row_count=len(rows),
        artifact=str(payload.get("artifact", "T02_High_Confidence_Network.csv")),
        checksum="sha256:golden",
        filters={},
        sort=None,
        error_code=None,
    )


def _state(focus_job_ids: Sequence[str]) -> RunState:
    return RunState(
        run_id="eval-run", user_id="eval-user", thread_id="eval-thread",
        active_profile=ActiveProfile.INTERPRETATION if focus_job_ids else ActiveProfile.ANALYSIS,
        state=AgentState.AWAIT_FOLLOWUP if focus_job_ids else AgentState.COLLECT_INTENT,
        step_no=0, plan_id=None, plan_hash=None, pending_approval_id=None,
        focus=RunFocus(in_scope_job_ids=list(focus_job_ids), resolved_entities={}, last_citation=None),
        model_calls=0, tool_calls=0, status=RunStatus.RUNNING, version=0,
    )


def _mismatches(actual: Mapping[str, object], expected: Mapping[str, object]) -> list[str]:
    return [
        f"{key}: expected {expected_value!r}, got {actual.get(key)!r}"
        for key, expected_value in expected.items()
        if actual.get(key) != expected_value
    ]


def _summarize(results: Sequence[EvalCaseResult]) -> EvalSummary:
    passed = sum(item.status is EvalCaseStatus.PASSED for item in results)
    failed = sum(item.status is EvalCaseStatus.FAILED for item in results)
    skipped = sum(item.status is EvalCaseStatus.SKIPPED for item in results)
    return EvalSummary(
        total=len(results),
        passed=passed,
        failed=failed,
        skipped=skipped,
        pass_rate=_pass_rate(results),
        model_calls=sum(item.model_calls for item in results),
        p95_latency_ms=_p95(results),
        metrics=_metrics(results),
    )


def _pass_rate(results: Sequence[EvalCaseResult]) -> float:
    attempted = [item for item in results if item.status is not EvalCaseStatus.SKIPPED]
    if not attempted:
        return 0.0
    return sum(item.status is EvalCaseStatus.PASSED for item in attempted) / len(attempted)


def _p95(results: Sequence[EvalCaseResult]) -> float:
    durations = [item.duration_ms for item in results if item.status is not EvalCaseStatus.SKIPPED]
    if not durations:
        return 0.0
    if len(durations) < 2:
        return durations[0]
    return quantiles(durations, n=100, method="inclusive")[94]


def _metrics(results: Sequence[EvalCaseResult]) -> dict[str, float | None]:
    def category_rate(category: EvalCategory) -> float | None:
        selected = [item for item in results if item.category is category and item.status is not EvalCaseStatus.SKIPPED]
        return None if not selected else sum(item.status is EvalCaseStatus.PASSED for item in selected) / len(selected)

    def safety_count(result: EvalCaseResult | None) -> float | None:
        if result is None or result.status is EvalCaseStatus.SKIPPED:
            return None
        return 0.0 if result.status is EvalCaseStatus.PASSED else 1.0

    cross_user = next((item for item in results if item.case_id == "ground_cross_user_404_001"), None)
    unapproved = next((item for item in results if item.case_id == "contrast_unapproved_write_001"), None)
    schema_results = [item.schema_valid for item in results if item.schema_valid is not None]
    return {
        "schema_validity": None if not schema_results else sum(schema_results) / len(schema_results),
        "route_accuracy": category_rate(EvalCategory.ROUTER),
        "recommendation_accuracy": category_rate(EvalCategory.RECOMMENDATION),
        "contrast_block_rate": category_rate(EvalCategory.CONTRAST),
        "unapproved_job_creations": safety_count(unapproved),
        "cross_user_access_successes": safety_count(cross_user),
        "numeric_accuracy": category_rate(EvalCategory.GROUNDING),
        "citation_coverage": category_rate(EvalCategory.GROUNDING),
    }


def _metric_delta(baseline: float | None, candidate: float | None) -> float | None:
    if baseline is None or candidate is None:
        return None
    return candidate - baseline
