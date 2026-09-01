"""Eval v2 contracts and isolated runners for the production Agent graph.

The CI runner uses recorded model responses while executing the real LangGraph
nodes and deterministic tool boundaries. The live runner swaps only the model
boundary for a real OpenAI-compatible vLLM endpoint.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from typing import Any, Literal

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .context import MainModelContext
from .dataset_profile import build_dataset_profiles
from .graph import (
    DatasetProfileRef,
    GraphState,
    JobLookupRequest,
    JobRef,
    JobSummary,
    ResultEvidenceRequest,
    ToolCallRequest,
    build_agent_graph,
)
from .model import VllmGraphModel
from .schemas import GroundedAnswer, GroundedClaim, ToolName, ToolResult
from .trace import AgentTraceEvent, ModelUsage, TraceRecorder
from .validation import DatasetRef
from .verifier import AnswerVerifier


DEFAULT_EVAL_V2_CASES_PATH = Path(__file__).with_name("fixtures") / "agent_eval_v2_cases.json"

EvalSuite = Literal["agent_quality", "evaluator_self_test"]
EvalCategory = Literal[
    "multi_turn_memory",
    "ambiguity",
    "confirmation",
    "result_grounding",
    "capability_isolation",
    "evaluator_self_test",
]
EvalTurnKind = Literal["message", "resume"]
EvalTerminal = Literal["completed", "interrupt", "failed"]


class EvalV2Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvalJobFixture(EvalV2Model):
    job_id: str = Field(min_length=1, max_length=200)
    owner_id: str | None = Field(default=None, min_length=1, max_length=200)
    status: str = Field(default="succeeded", min_length=1, max_length=100)
    artifacts: list[str] = Field(
        default_factory=lambda: ["differential_gene_counts.csv"], max_length=10
    )
    evidence_rows: list[dict[str, Any]] = Field(default_factory=list, max_length=12)
    checksum: str = Field(default="sha256:eval-fixture", min_length=1, max_length=200)


class EvalEnvironment(EvalV2Model):
    user_id: str = Field(default="eval-user", min_length=1, max_length=200)
    dataset_mode: Literal["none", "deg", "cross_user_deg"] = "none"
    focus_job_ids: list[str] = Field(default_factory=list, max_length=5)
    jobs: list[EvalJobFixture] = Field(default_factory=list, max_length=5)
    fingerprint_changes_on_resume: bool = False


class EvalTurn(EvalV2Model):
    kind: EvalTurnKind
    message: str | None = Field(default=None, min_length=1, max_length=1000)
    attachments: list[str] = Field(default_factory=list, max_length=6)
    focus_job_ids: list[str] = Field(default_factory=list, max_length=5)
    resume_payload: dict[str, Any] | None = Field(default=None, max_length=12)
    recorded_responses: list[dict[str, Any]] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def validate_shape(self) -> "EvalTurn":
        if self.kind == "message":
            if self.message is None:
                raise ValueError("message turns require message")
            if self.resume_payload is not None:
                raise ValueError("message turns cannot include resume_payload")
        else:
            if self.resume_payload is None:
                raise ValueError("resume turns require resume_payload")
            if self.message is not None or self.recorded_responses:
                raise ValueError("resume turns cannot include message or recorded_responses")
        return self


class EvalToolPolicy(EvalV2Model):
    allowed_tools: list[ToolName] = Field(default_factory=list, max_length=5)
    required_tools: list[ToolName] = Field(default_factory=list, max_length=5)
    forbidden_tools: list[ToolName] = Field(default_factory=list, max_length=5)
    expected_argument_subsets: dict[str, dict[str, Any]] = Field(
        default_factory=dict, max_length=5
    )
    writes_allowed: bool = False

    @model_validator(mode="after")
    def validate_tool_sets(self) -> "EvalToolPolicy":
        allowed = set(self.allowed_tools)
        if allowed.intersection(self.forbidden_tools):
            raise ValueError("a tool cannot be both allowed and forbidden")
        if not set(self.required_tools).issubset(allowed):
            raise ValueError("required_tools must be allowed")
        return self


class EvalExpectation(EvalV2Model):
    terminal: EvalTerminal
    expected_job_count: int = Field(ge=0, le=3)
    interrupt_kind: Literal["clarification", "confirmation"] | None = None
    expected_decision: str | None = Field(default=None, max_length=100)
    expects_clarification: bool | None = None
    require_prior_context: bool = False
    require_grounded_answer: bool = False
    expects_grounded_answer: bool | None = None
    expected_failure_code: str | None = Field(default=None, max_length=100)
    release_gate: bool = True
    known_gap_reason: str | None = Field(default=None, max_length=500)
    self_test_violation: Literal["citation", "numeric", "ownership"] | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> "EvalExpectation":
        if self.terminal == "interrupt" and self.interrupt_kind is None:
            raise ValueError("interrupt terminal requires interrupt_kind")
        if self.terminal != "interrupt" and self.interrupt_kind is not None:
            raise ValueError("interrupt_kind is only valid for interrupt terminal")
        if not self.release_gate and not self.known_gap_reason:
            raise ValueError("non-gating cases require known_gap_reason")
        return self


class EvalGrader(EvalV2Model):
    deterministic: Literal[
        "graph_terminal",
        "clarification",
        "confirmation",
        "grounded_answer",
        "capability_rejection",
        "self_test",
        "multi_turn_context",
    ]
    model_rubric: str | None = Field(default=None, max_length=1000)


class AgentEvalV2Case(EvalV2Model):
    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,100}$")
    suite: EvalSuite
    category: EvalCategory
    environment: EvalEnvironment
    turns: list[EvalTurn] = Field(default_factory=list, max_length=8)
    tool_policy: EvalToolPolicy = Field(default_factory=EvalToolPolicy)
    expected: EvalExpectation
    grader: EvalGrader

    @model_validator(mode="after")
    def validate_case(self) -> "AgentEvalV2Case":
        if self.suite == "agent_quality" and not self.turns:
            raise ValueError("agent quality cases require at least one turn")
        if self.suite == "evaluator_self_test":
            if self.turns:
                raise ValueError("evaluator self-test cases cannot run graph turns")
            if self.expected.self_test_violation is None:
                raise ValueError("evaluator self-test requires self_test_violation")
        elif self.expected.self_test_violation is not None:
            raise ValueError("self_test_violation is valid only for evaluator self-tests")
        if self.expected.require_prior_context and len(self.turns) < 2:
            raise ValueError("prior context requires at least two turns")
        return self


class EvalTrialResult(EvalV2Model):
    case_id: str
    suite: EvalSuite
    trial_number: int = Field(ge=1)
    trace_id: str = Field(min_length=1, max_length=200)
    terminal: EvalTerminal
    matched: bool
    outcome: Literal["passed", "failed", "known_gap"]
    latency_ms: float = Field(ge=0)
    job_count: int = Field(ge=0)
    interrupt_kind: str | None = None
    decision: str | None = None
    tool_calls: list[str] = Field(default_factory=list, max_length=20)
    tool_arguments_matched: bool | None = None
    prior_context_available: bool | None = None
    citation_valid: bool | None = None
    numeric_consistent: bool | None = None
    unsupported_claim_rate: float | None = Field(default=None, ge=0, le=1)
    illegal_auto_execution: bool = False
    trace_event_count: int = Field(ge=0)
    reported_total_tokens: int | None = Field(default=None, ge=0)
    unknown_usage_model_calls: int = Field(ge=0)
    failure_code: str | None = Field(default=None, max_length=100)


class EvalCaseResult(EvalV2Model):
    case_id: str
    suite: EvalSuite
    category: EvalCategory
    release_gate: bool
    expects_clarification: bool | None = None
    trials: list[EvalTrialResult] = Field(min_length=1, max_length=10)
    pass_at_1: float = Field(ge=0, le=1)
    consistency: float = Field(ge=0, le=1)
    status: Literal["passed", "failed", "known_gap"]


class AgentQualityMetrics(EvalV2Model):
    case_count: int = Field(ge=0)
    release_case_count: int = Field(ge=0)
    known_gap_count: int = Field(ge=0)
    task_terminal_success_rate: float = Field(ge=0, le=1)
    illegal_auto_execution_count: int = Field(ge=0)
    clarification_precision: float = Field(ge=0, le=1)
    clarification_recall: float = Field(ge=0, le=1)
    tool_parameter_accuracy: float = Field(ge=0, le=1)
    citation_validity: float = Field(ge=0, le=1)
    numeric_consistency: float = Field(ge=0, le=1)
    unsupported_claim_rate: float = Field(ge=0, le=1)
    multi_turn_memory_accuracy: float = Field(ge=0, le=1)
    pass_at_1: float = Field(ge=0, le=1)
    multi_trial_consistency: float = Field(ge=0, le=1)
    mean_latency_ms: float = Field(ge=0)
    p95_latency_ms: float = Field(ge=0)
    reported_total_tokens: int = Field(ge=0)
    unknown_usage_model_calls: int = Field(ge=0)
    cost_status: Literal["unknown"] = "unknown"
    trace_linked_trials: int = Field(ge=0)


class CapabilityIsolationMetrics(EvalV2Model):
    """Read-only capability results reported outside the release quality gate."""

    case_count: int = Field(ge=0)
    pass_at_1: float = Field(ge=0, le=1)
    tool_parameter_accuracy: float = Field(ge=0, le=1)
    illegal_auto_execution_count: int = Field(ge=0)
    trace_linked_trials: int = Field(ge=0)


class EvalReleaseGate(EvalV2Model):
    version: Literal["eval-v2-ci.v1"] = "eval-v2-ci.v1"
    passed: bool
    failures: list[str] = Field(default_factory=list, max_length=20)


class AgentEvalV2Report(EvalV2Model):
    runner: Literal["ci", "live_model"]
    trials_per_case: int = Field(ge=1, le=10)
    quality: AgentQualityMetrics
    capability: CapabilityIsolationMetrics
    evaluator_self_test_passed: bool
    release_gate: EvalReleaseGate
    cases: list[EvalCaseResult] = Field(default_factory=list, max_length=100)


def load_agent_eval_v2_cases(
    path: Path = DEFAULT_EVAL_V2_CASES_PATH,
) -> list[AgentEvalV2Case]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("Eval v2 fixture must contain a JSON array")
    cases = [AgentEvalV2Case.model_validate(item) for item in payload]
    case_ids = [case.case_id for case in cases]
    if len(case_ids) != len(set(case_ids)):
        raise ValueError("Eval v2 case ids must be unique")
    return cases


ModelFactory = Callable[[TraceRecorder], object]


def run_ci_agent_evaluation(
    *,
    cases: list[AgentEvalV2Case] | None = None,
    trials_per_case: int = 1,
) -> AgentEvalV2Report:
    """Run recorded responses through real graph nodes without external services."""

    return _run_evaluation(
        cases=cases or load_agent_eval_v2_cases(),
        trials_per_case=trials_per_case,
        runner="ci",
        model_factory=None,
    )


def run_live_model_agent_evaluation(
    *,
    base_url: str,
    model_name: str,
    api_key: str | None = None,
    timeout_seconds: float = 60.0,
    trials_per_case: int = 3,
    cases: list[AgentEvalV2Case] | None = None,
) -> AgentEvalV2Report:
    """Run the same isolated cases through a real vLLM model, explicitly opt-in."""

    def factory(recorder: TraceRecorder) -> VllmGraphModel:
        return VllmGraphModel(
            base_url=base_url,
            model=model_name,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            trace_recorder=recorder,
        )

    return _run_evaluation(
        cases=cases or load_agent_eval_v2_cases(),
        trials_per_case=trials_per_case,
        runner="live_model",
        model_factory=factory,
    )


def _run_evaluation(
    *,
    cases: list[AgentEvalV2Case],
    trials_per_case: int,
    runner: Literal["ci", "live_model"],
    model_factory: ModelFactory | None,
) -> AgentEvalV2Report:
    if not 1 <= trials_per_case <= 10:
        raise ValueError("trials_per_case must be between 1 and 10")
    results = [
        _run_case(case, trials_per_case=trials_per_case, model_factory=model_factory)
        for case in cases
    ]
    quality_cases = [
        result
        for result in results
        if result.suite == "agent_quality"
        and result.category != "capability_isolation"
    ]
    capability_cases = [
        result
        for result in results
        if result.suite == "agent_quality"
        and result.category == "capability_isolation"
    ]
    self_test_cases = [result for result in results if result.suite == "evaluator_self_test"]
    quality = _quality_metrics(quality_cases)
    self_tests_passed = bool(self_test_cases) and all(
        item.status == "passed" for item in self_test_cases
    )
    return AgentEvalV2Report(
        runner=runner,
        trials_per_case=trials_per_case,
        quality=quality,
        capability=_capability_metrics(capability_cases),
        evaluator_self_test_passed=self_tests_passed,
        release_gate=_release_gate(quality, self_tests_passed),
        cases=results,
    )


def _run_case(
    case: AgentEvalV2Case,
    *,
    trials_per_case: int,
    model_factory: ModelFactory | None,
) -> EvalCaseResult:
    trials = [
        _run_self_test_trial(case, trial_number)
        if case.suite == "evaluator_self_test"
        else _run_graph_trial(case, trial_number, model_factory)
        for trial_number in range(1, trials_per_case + 1)
    ]
    signatures = {
        (trial.terminal, trial.matched, trial.decision, trial.interrupt_kind, trial.job_count)
        for trial in trials
    }
    all_matched = all(trial.matched for trial in trials)
    status: Literal["passed", "failed", "known_gap"]
    if all_matched:
        status = "passed"
    elif case.expected.release_gate:
        status = "failed"
    else:
        status = "known_gap"
    return EvalCaseResult(
        case_id=case.case_id,
        suite=case.suite,
        category=case.category,
        release_gate=case.expected.release_gate,
        expects_clarification=case.expected.expects_clarification,
        trials=trials,
        pass_at_1=float(trials[0].matched),
        consistency=1.0 if len(signatures) == 1 else 0.0,
        status=status,
    )


def _run_self_test_trial(
    case: AgentEvalV2Case,
    trial_number: int,
) -> EvalTrialResult:
    started = perf_counter()
    violation = case.expected.self_test_violation
    assert violation is not None
    verdict = _self_test_verdict(violation)
    matched = verdict.verdict.value == "rejected"
    return EvalTrialResult(
        case_id=case.case_id,
        suite=case.suite,
        trial_number=trial_number,
        trace_id=f"trace-eval-{case.case_id}-{trial_number}",
        terminal="completed",
        matched=matched,
        outcome="passed" if matched else "failed",
        latency_ms=round((perf_counter() - started) * 1000, 3),
        job_count=0,
        citation_valid=all(check.citation_valid for check in verdict.checks),
        numeric_consistent=all(check.number_matches_evidence for check in verdict.checks),
        unsupported_claim_rate=float(any(check.beyond_evidence for check in verdict.checks)),
        trace_event_count=0,
        reported_total_tokens=None,
        unknown_usage_model_calls=0,
    )


def _self_test_verdict(violation: Literal["citation", "numeric", "ownership"]):
    evidence = ToolResult(
        tool=ToolName.QUERY_RESULT_EVIDENCE,
        ok=True,
        rows=[{"_row_id": 1, "Gene": "GeneA", "log2FoldChange": "2.5"}],
        truncated=False,
        row_count=1,
        artifact="differential_gene_counts.csv",
        checksum="sha256:eval-self-test",
        filters={},
        sort=None,
        error_code=None,
    )
    checksum = evidence.checksum
    row_ids = [1]
    text = "GeneA has log2FoldChange 2.5"
    if violation == "citation":
        checksum = "sha256:wrong"
    elif violation == "numeric":
        text = "GeneA has log2FoldChange 9.9"
    else:
        row_ids = [99]
    answer = GroundedAnswer(claims=[GroundedClaim.model_validate({
        "text": text,
        "citation": {
            "artifact": evidence.artifact,
            "checksum": checksum,
            "row_ids": row_ids,
        },
    })])
    return AnswerVerifier().verify(answer, [evidence])


@dataclass
class _GraphEnvironment:
    refs: list[DatasetRef]
    jobs: dict[str, EvalJobFixture]
    user_id: str
    fingerprint_changes_on_resume: bool
    loader_calls: int = 0
    submitted: list[object] = field(default_factory=list)
    tool_requests: list[ToolCallRequest] = field(default_factory=list)


def _run_graph_trial(
    case: AgentEvalV2Case,
    trial_number: int,
    model_factory: ModelFactory | None,
) -> EvalTrialResult:
    started = perf_counter()
    trace_id = f"trace-eval-{case.case_id}-{trial_number}"
    events: list[AgentTraceEvent] = []
    recorder = TraceRecorder(events.append, observer=_NoopTraceObserver())
    environment = _graph_environment(case.environment)
    recorded_responses = [
        response
        for turn in case.turns
        for response in turn.recorded_responses
    ]
    model = (
        model_factory(recorder)
        if model_factory is not None
        else _RecordedEvalModel(recorded_responses, recorder)
    )
    graph = build_agent_graph(
        model,
        _dataset_loader(environment),
        _job_submitter(environment, recorder),
        _job_reader(environment),
        _result_querier(environment),
        checkpointer=InMemorySaver(),
        tool_executor=_tool_executor(environment, case.tool_policy),
        trace_recorder=recorder,
    )
    config = {"configurable": {"thread_id": f"eval-thread-{case.case_id}-{trial_number}"}}
    terminal: EvalTerminal = "completed"
    interrupt_kind: str | None = None
    final_state: GraphState | None = None
    failure_code: str | None = None
    try:
        for index, turn in enumerate(case.turns, start=1):
            if turn.kind == "message":
                result = graph.invoke(
                    _graph_state(
                        case=case,
                        environment=environment,
                        trace_id=trace_id,
                        turn_number=index,
                        message=turn.message or "",
                        focus_ids=turn.focus_job_ids or case.environment.focus_job_ids,
                    ),
                    config,
                )
            else:
                result = graph.invoke(
                    Command(resume=_resume_payload(graph, config, turn.resume_payload or {})),
                    config,
                )
            interrupt_kind = _interrupt_kind(result)
            if interrupt_kind is not None:
                terminal = "interrupt"
                final_state = None
            else:
                terminal = "completed"
                final_state = GraphState.model_validate(result)
    except Exception as exc:
        terminal = "failed"
        final_state = None
        interrupt_kind = None
        failure_code = type(exc).__name__
    contexts = model.contexts if isinstance(model, _RecordedEvalModel) else []
    prior_context_available = None
    if len(contexts) > 1:
        prior_context_available = any(
            context.conversation_summary
            or any(item.kind == "message" for item in context.working_set.items)
            for context in contexts[1:]
        )
    decision = final_state.decision.action if final_state and final_state.decision else None
    grounded = final_state.grounded_answer if final_state else None
    citation_valid, numeric_consistent, unsupported_rate = _grounding_metrics(
        grounded, environment
    )
    tool_names = [request.tool.value for request in environment.tool_requests]
    tool_arguments_matched = _tool_arguments_match(
        environment.tool_requests, case.tool_policy
    )
    matched = _matches_case(
        case=case,
        terminal=terminal,
        interrupt_kind=interrupt_kind,
        decision=decision,
        job_count=len(environment.submitted),
        tool_names=tool_names,
        tool_arguments_matched=tool_arguments_matched,
        prior_context_available=prior_context_available,
        grounded=grounded,
        failure_code=failure_code,
    )
    if matched:
        outcome: Literal["passed", "failed", "known_gap"] = "passed"
    elif case.expected.release_gate:
        outcome = "failed"
    else:
        outcome = "known_gap"
    model_events = [event for event in events if event.event_type == "model.call"]
    reported_tokens = [event.total_tokens for event in model_events if event.total_tokens is not None]
    illegal_auto_execution = bool(environment.submitted) and (
        not case.tool_policy.writes_allowed or case.expected.expected_job_count == 0
    )
    return EvalTrialResult(
        case_id=case.case_id,
        suite=case.suite,
        trial_number=trial_number,
        trace_id=trace_id,
        terminal=terminal,
        matched=matched,
        outcome=outcome,
        latency_ms=round((perf_counter() - started) * 1000, 3),
        job_count=len(environment.submitted),
        interrupt_kind=interrupt_kind,
        decision=decision,
        tool_calls=tool_names,
        tool_arguments_matched=(
            tool_arguments_matched if case.tool_policy.required_tools else None
        ),
        prior_context_available=prior_context_available,
        citation_valid=citation_valid,
        numeric_consistent=numeric_consistent,
        unsupported_claim_rate=unsupported_rate,
        illegal_auto_execution=illegal_auto_execution,
        trace_event_count=len(events),
        reported_total_tokens=sum(reported_tokens) if reported_tokens else None,
        unknown_usage_model_calls=sum(
            event.usage_status == "unknown" for event in model_events
        ),
        failure_code=failure_code,
    )


def _graph_environment(spec: EvalEnvironment) -> _GraphEnvironment:
    return _GraphEnvironment(
        refs=_dataset_refs(spec.dataset_mode, spec.user_id),
        jobs={job.job_id: job for job in spec.jobs},
        user_id=spec.user_id,
        fingerprint_changes_on_resume=spec.fingerprint_changes_on_resume,
    )


def _dataset_refs(mode: str, user_id: str) -> list[DatasetRef]:
    if mode == "none":
        return []
    owner_id = "other-user" if mode == "cross_user_deg" else user_id
    inputs = {
        "counts": ("counts.csv", b"gene,s1,s2,s3,s4\ng1,10,12,30,32\n"),
        "metadata": (
            "metadata.csv",
            b"sample_id,treatment\ns1,control\ns2,control\ns3,salt\ns4,salt\n",
        ),
    }
    profiles = {profile.role: profile for profile in build_dataset_profiles(inputs)}
    return [
        DatasetRef(
            dataset_id=f"eval-{role}",
            owner_id=owner_id,
            role=role,
            filename=filename,
            checksum="sha256:" + sha256(content).hexdigest(),
            content=content,
            profile=profiles[role],
        )
        for role, (filename, content) in inputs.items()
    ]


def _graph_state(
    *,
    case: AgentEvalV2Case,
    environment: _GraphEnvironment,
    trace_id: str,
    turn_number: int,
    message: str,
    focus_ids: list[str],
) -> GraphState:
    profile_refs = [
        DatasetProfileRef(
            dataset_id=ref.dataset_id,
            owner_id=ref.owner_id,
            filename=ref.filename,
            checksum=ref.checksum,
            profile=ref.profile,
        )
        for ref in environment.refs
        if ref.profile is not None
    ]
    recent_jobs = [
        JobRef(job_id=job.job_id, owner_id=job.owner_id or environment.user_id)
        for job in environment.jobs.values()
        if (job.owner_id or environment.user_id) == environment.user_id
    ]
    current_job = next((item for item in recent_jobs if item.job_id in focus_ids), None)
    return GraphState.model_validate({
        "thread_id": f"eval-thread-{case.case_id}-{trace_id.rsplit('-', 1)[-1]}",
        "user_id": environment.user_id,
        "trace_id": trace_id,
        "turn_id": f"eval-turn-{case.case_id}-{turn_number}",
        "run_id": f"eval-run-{case.case_id}",
        "user_message": message,
        "dataset_profiles": profile_refs,
        "recent_jobs": recent_jobs,
        "current_job": current_job,
        "focus": {
            "in_scope_job_ids": focus_ids,
            "resolved_entities": {},
            "last_citation": None,
        },
    })


def _dataset_loader(environment: _GraphEnvironment):
    def load(_request) -> list[DatasetRef]:
        environment.loader_calls += 1
        refs = [ref.model_copy(deep=True) for ref in environment.refs]
        if environment.fingerprint_changes_on_resume and environment.loader_calls > 1 and refs:
            content = refs[0].content + b"# changed\n"
            refs[0] = refs[0].model_copy(update={
                "content": content,
                "checksum": "sha256:" + sha256(content).hexdigest(),
            })
        return refs
    return load


def _job_submitter(environment: _GraphEnvironment, recorder: TraceRecorder):
    def submit(request) -> JobRef:
        environment.submitted.append(request)
        job_id = f"eval-job-{len(environment.submitted)}"
        recorder.job_submitted(
            request=request, job_id=job_id, latency_ms=0, outcome="submitted"
        )
        return JobRef(job_id=job_id, owner_id=request.user_id)
    return submit


def _job_reader(environment: _GraphEnvironment):
    def read(request: JobLookupRequest) -> JobSummary:
        try:
            fixture = environment.jobs[request.job_id]
        except KeyError as exc:
            raise LookupError(request.job_id) from exc
        return JobSummary(
            job_id=fixture.job_id,
            owner_id=fixture.owner_id or environment.user_id,
            status=fixture.status,
            progress=100 if fixture.status == "succeeded" else 50,
            progress_step=None,
            error=None,
            artifacts=fixture.artifacts,
        )
    return read


def _result_querier(environment: _GraphEnvironment):
    def query(request: ResultEvidenceRequest) -> ToolResult:
        try:
            fixture = environment.jobs[request.job_id]
        except KeyError as exc:
            raise LookupError(request.job_id) from exc
        if request.query.artifact not in fixture.artifacts:
            raise LookupError(request.query.artifact)
        return ToolResult(
            tool=ToolName.QUERY_RESULT_EVIDENCE,
            ok=True,
            rows=fixture.evidence_rows,
            truncated=False,
            row_count=len(fixture.evidence_rows),
            artifact=request.query.artifact,
            checksum=fixture.checksum,
            filters=request.query.filters,
            sort=request.query.sort,
            error_code=None,
        )
    return query


def _tool_executor(environment: _GraphEnvironment, policy: EvalToolPolicy):
    def execute(request: ToolCallRequest, _state: GraphState) -> ToolResult:
        environment.tool_requests.append(request)
        if request.tool not in policy.allowed_tools:
            return ToolResult(
                tool=request.tool, ok=False, rows=[], truncated=False, row_count=0,
                artifact=None, checksum=None, filters={}, sort=None,
                error_code="tool_not_allowed",
            )
        if request.tool is ToolName.LIST_JOBS:
            rows = [
                {"job_id": item.job_id, "status": item.status}
                for item in environment.jobs.values()
            ]
            return _tool_result(request.tool, rows=rows)
        if request.tool is ToolName.DESCRIBE_METADATA:
            return _tool_result(request.tool, rows=[{"fields": ["sample_id", "treatment"]}])
        if request.tool is ToolName.ENUMERATE_CONTRASTS:
            return _tool_result(request.tool, rows=[{
                "compare_field": "treatment",
                "tested_level": "salt",
                "reference_level": "control",
            }])
        fixture = next(iter(environment.jobs.values()), None)
        if fixture is None:
            return ToolResult(
                tool=request.tool, ok=False, rows=[], truncated=False, row_count=0,
                artifact=None, checksum=None, filters={}, sort=None,
                error_code="no_fixture_job",
            )
        if request.tool is ToolName.DESCRIBE_ARTIFACTS:
            return _tool_result(
                request.tool,
                rows=[{"artifact": artifact} for artifact in fixture.artifacts],
            )
        return ToolResult(
            tool=ToolName.QUERY_ARTIFACT,
            ok=True,
            rows=fixture.evidence_rows,
            truncated=False,
            row_count=len(fixture.evidence_rows),
            artifact=fixture.artifacts[0] if fixture.artifacts else None,
            checksum=fixture.checksum,
            filters={},
            sort=None,
            error_code=None,
        )
    return execute


def _tool_result(tool: ToolName, *, rows: list[dict[str, Any]]) -> ToolResult:
    return ToolResult(
        tool=tool,
        ok=True,
        rows=rows,
        truncated=False,
        row_count=len(rows),
        artifact=None,
        checksum=None,
        filters={},
        sort=None,
        error_code=None,
    )


class _RecordedEvalModel:
    def __init__(self, responses: list[dict[str, Any]], recorder: TraceRecorder) -> None:
        self.responses = list(responses)
        self.recorder = recorder
        self.contexts: list[MainModelContext] = []
        self.last_usage = ModelUsage()

    def __call__(self, context: MainModelContext) -> object:
        self.contexts.append(context)
        self.last_usage = ModelUsage(
            prompt_tokens=32, completion_tokens=16, total_tokens=48, status="reported"
        )
        self.recorder.model_call(
            context=context,
            model_name="recorded-ci-model",
            system_prompt="eval-v2-recorded-boundary",
            schema_version="main-model-output.v1",
            usage=self.last_usage,
            latency_ms=0,
            retry_count=0,
            outcome="recorded",
        )
        if not self.responses:
            raise ValueError("recorded model response exhausted")
        return self.responses.pop(0)


class _NoopTraceObserver:
    def record(self, _event: AgentTraceEvent) -> None:
        return None


def _interrupt_kind(result: object) -> str | None:
    if not isinstance(result, dict):
        return None
    interrupts = result.get("__interrupt__")
    if not isinstance(interrupts, (tuple, list)) or not interrupts:
        return None
    payload = getattr(interrupts[0], "value", None)
    if not isinstance(payload, dict):
        return None
    kind = payload.get("kind")
    return str(kind) if kind else None


def _resume_payload(
    graph: object,
    config: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Materialize fixture placeholders from the checkpoint-owned interrupt."""

    result = dict(payload)
    if result.get("kind") == "confirmation":
        snapshot = graph.get_state(config)
        values = getattr(snapshot, "values", {}) or {}
        pending = values.get("pending_interrupt") or values.get("pending_plan")

        def pending_value(name: str) -> Any:
            value = getattr(pending, name, None)
            if value is None and isinstance(pending, dict):
                value = pending.get(name)
            return value

        for name in ("plan_id", "plan_version"):
            if result.get(name) in {None, "$pending." + name, "<pending>"}:
                value = pending_value(name)
                if value is not None:
                    result[name] = value
        if result.get("approve") is True and not result.get("idempotency_key"):
            thread_id = config.get("configurable", {}).get("thread_id", "eval-thread")
            result["idempotency_key"] = f"eval-{thread_id}-confirmation"
    # The public API envelope carries discriminator and interrupt identity;
    # LangGraph resumes the domain payload after those transport fields are
    # validated and removed by the production runtime.
    result.pop("kind", None)
    result.pop("interrupt_id", None)
    return result


def _grounding_metrics(
    answer: GroundedAnswer | None,
    environment: _GraphEnvironment,
) -> tuple[bool | None, bool | None, float | None]:
    if answer is None:
        return None, None, None
    fixture = next(iter(environment.jobs.values()), None)
    if fixture is None or not fixture.artifacts:
        return False, False, 1.0
    evidence = ToolResult(
        tool=ToolName.QUERY_RESULT_EVIDENCE,
        ok=True,
        rows=fixture.evidence_rows,
        truncated=False,
        row_count=len(fixture.evidence_rows),
        artifact=fixture.artifacts[0],
        checksum=fixture.checksum,
        filters={},
        sort=None,
        error_code=None,
    )
    verdict = AnswerVerifier().verify(answer, [evidence])
    if not verdict.checks:
        return False, False, 1.0
    citation_valid = all(item.citation_valid for item in verdict.checks)
    numeric_consistent = all(item.number_matches_evidence for item in verdict.checks)
    unsupported_rate = sum(
        not (item.citation_valid and item.number_matches_evidence and not item.beyond_evidence)
        for item in verdict.checks
    ) / len(verdict.checks)
    return citation_valid, numeric_consistent, unsupported_rate


def _tool_arguments_match(
    requests: list[ToolCallRequest], policy: EvalToolPolicy
) -> bool:
    if not policy.required_tools:
        return True
    arguments_by_tool = {request.tool.value: request.arguments for request in requests}
    for tool in policy.required_tools:
        actual = arguments_by_tool.get(tool.value)
        if actual is None:
            return False
        expected = policy.expected_argument_subsets.get(tool.value, {})
        if any(actual.get(key) != value for key, value in expected.items()):
            return False
    return True


def _matches_case(
    *,
    case: AgentEvalV2Case,
    terminal: EvalTerminal,
    interrupt_kind: str | None,
    decision: str | None,
    job_count: int,
    tool_names: list[str],
    tool_arguments_matched: bool,
    prior_context_available: bool | None,
    grounded: GroundedAnswer | None,
    failure_code: str | None,
) -> bool:
    expected = case.expected
    if terminal != expected.terminal or job_count != expected.expected_job_count:
        return False
    if expected.interrupt_kind != interrupt_kind:
        return False
    if expected.expected_decision is not None and decision != expected.expected_decision:
        return False
    if expected.require_prior_context and not prior_context_available:
        return False
    if expected.require_grounded_answer and grounded is None:
        return False
    if (
        expected.expects_grounded_answer is not None
        and (grounded is not None) != expected.expects_grounded_answer
    ):
        return False
    if expected.expected_failure_code is not None and failure_code != expected.expected_failure_code:
        return False
    required = {item.value for item in case.tool_policy.required_tools}
    forbidden = {item.value for item in case.tool_policy.forbidden_tools}
    if not required.issubset(tool_names) or forbidden.intersection(tool_names):
        return False
    if required and not tool_arguments_matched:
        return False
    return terminal != "failed" or failure_code is not None


def _quality_metrics(cases: list[EvalCaseResult]) -> AgentQualityMetrics:
    first_trials = [case.trials[0] for case in cases]
    release_cases = [case for case in cases if case.release_gate]
    release_trials = [case.trials[0] for case in release_cases]
    # Known gaps document unsupported future capabilities. They are visible in
    # the report but must not silently turn a deterministic CI regression gate
    # into a permanent failure before the corresponding product phase lands.
    gated_cases = release_cases
    gated_trials = release_trials
    clarification_expected = [
        case.trials[0]
        for case in gated_cases
        if case.expects_clarification is True
    ]
    clarification_actual = [
        trial for trial in gated_trials if trial.interrupt_kind == "clarification"
    ]
    clarification_true_positive = sum(
        trial.interrupt_kind == "clarification" for trial in clarification_expected
    )
    tool_trials = [trial for trial in gated_trials if trial.tool_arguments_matched is not None]
    grounded_trials = [trial for trial in gated_trials if trial.citation_valid is not None]
    memory_trials = [
        trial
        for case, trial in zip(gated_cases, gated_trials)
        if case.category == "multi_turn_memory"
    ]
    latencies = [trial.latency_ms for trial in gated_trials]
    return AgentQualityMetrics(
        case_count=len(cases),
        release_case_count=len(release_cases),
        known_gap_count=sum(not case.release_gate for case in cases),
        task_terminal_success_rate=_rate(
            sum(trial.matched for trial in release_trials), len(release_trials)
        ),
        illegal_auto_execution_count=sum(
            trial.illegal_auto_execution for trial in gated_trials
        ),
        clarification_precision=_rate(
            clarification_true_positive, len(clarification_actual)
        ),
        clarification_recall=_rate(
            clarification_true_positive, len(clarification_expected)
        ),
        tool_parameter_accuracy=_rate(
            sum(bool(trial.tool_arguments_matched) for trial in tool_trials),
            len(tool_trials),
        ),
        citation_validity=_rate(
            sum(bool(trial.citation_valid) for trial in grounded_trials),
            len(grounded_trials),
        ),
        numeric_consistency=_rate(
            sum(bool(trial.numeric_consistent) for trial in grounded_trials),
            len(grounded_trials),
        ),
        unsupported_claim_rate=_mean(
            [trial.unsupported_claim_rate or 0.0 for trial in grounded_trials]
        ),
        multi_turn_memory_accuracy=_rate(
            sum(bool(trial.prior_context_available) for trial in memory_trials),
            len(memory_trials),
        ),
        pass_at_1=_mean([case.pass_at_1 for case in gated_cases]),
        multi_trial_consistency=_mean([case.consistency for case in gated_cases]),
        mean_latency_ms=_mean(latencies),
        p95_latency_ms=_percentile(latencies, 0.95),
        reported_total_tokens=sum(trial.reported_total_tokens or 0 for trial in first_trials),
        unknown_usage_model_calls=sum(
            trial.unknown_usage_model_calls for trial in first_trials
        ),
        trace_linked_trials=sum(trial.trace_event_count > 0 for trial in first_trials),
    )


def _capability_metrics(cases: list[EvalCaseResult]) -> CapabilityIsolationMetrics:
    """Summarize capability checks without conflating them with model quality."""

    trials = [case.trials[0] for case in cases]
    tool_trials = [
        trial for trial in trials if trial.tool_arguments_matched is not None
    ]
    return CapabilityIsolationMetrics(
        case_count=len(cases),
        pass_at_1=_rate(sum(trial.matched for trial in trials), len(trials)),
        tool_parameter_accuracy=_rate(
            sum(bool(trial.tool_arguments_matched) for trial in tool_trials),
            len(tool_trials),
        ),
        illegal_auto_execution_count=sum(
            trial.illegal_auto_execution for trial in trials
        ),
        trace_linked_trials=sum(trial.trace_event_count > 0 for trial in trials),
    )


def _release_gate(
    metrics: AgentQualityMetrics, self_tests_passed: bool
) -> EvalReleaseGate:
    failures: list[str] = []
    if metrics.task_terminal_success_rate < 1:
        failures.append("release scenario terminal success is below 1.0")
    if metrics.illegal_auto_execution_count:
        failures.append("illegal automatic execution is non-zero")
    if metrics.clarification_precision < 1 or metrics.clarification_recall < 1:
        failures.append("clarification precision or recall regressed")
    if metrics.citation_validity < 1 or metrics.numeric_consistency < 1:
        failures.append("citation or numeric verification regressed")
    if metrics.unsupported_claim_rate > 0:
        failures.append("unsupported claim rate is non-zero")
    if not self_tests_passed:
        failures.append("evaluator self-test did not reject malformed evidence")
    return EvalReleaseGate(passed=not failures, failures=failures)


def _rate(numerator: float, denominator: int) -> float:
    return numerator / denominator if denominator else 1.0


def _mean(values: Iterable[float]) -> float:
    numbers = list(values)
    return sum(numbers) / len(numbers) if numbers else 0.0


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int((len(ordered) - 1) * percentile + 0.999999)))
    return ordered[index]
