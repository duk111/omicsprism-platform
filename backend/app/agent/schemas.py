from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ..models import AnalysisType


class ContractModel(BaseModel):
    """所有 agent 契约默认拒绝未声明字段。"""

    model_config = ConfigDict(extra="forbid")


class RouteIntent(str, Enum):
    ANALYZE = "analyze"
    INTERPRET = "interpret"
    RERUN = "rerun"
    DESCRIBE_ONLY = "describe_only"
    UNCLEAR = "unclear"


class RouteTargetProfile(str, Enum):
    ANALYSIS = "analysis"
    INTERPRETATION = "interpretation"
    ASK_USER = "ask_user"


class ActiveProfile(str, Enum):
    ANALYSIS = "analysis"
    INTERPRETATION = "interpretation"


class AgentAction(str, Enum):
    PROPOSE_PLAN = "propose_plan"
    REQUEST_MORE_DATA = "request_more_data"
    RUN_PREFLIGHT = "run_preflight"
    REQUEST_APPROVAL = "request_approval"
    ANSWER = "answer"
    DIAGNOSE_FAILURE = "diagnose_failure"


class FeasibilityVerdict(str, Enum):
    ANSWERABLE = "answerable"
    ANSWERABLE_WITH_CAVEATS = "answerable_with_caveats"
    NOT_ANSWERABLE = "not_answerable"


class AgentState(str, Enum):
    COLLECT_INTENT = "COLLECT_INTENT"
    CHECK_INPUTS = "CHECK_INPUTS"
    PROPOSE_PLAN = "PROPOSE_PLAN"
    WAIT_PLAN_CONFIRMATION = "WAIT_PLAN_CONFIRMATION"
    PREFLIGHT = "PREFLIGHT"
    WAIT_EXECUTION_CONFIRMATION = "WAIT_EXECUTION_CONFIRMATION"
    SUBMIT_JOBS = "SUBMIT_JOBS"
    MONITOR_JOBS = "MONITOR_JOBS"
    ANSWER_WITH_EVIDENCE = "ANSWER_WITH_EVIDENCE"
    AWAIT_FOLLOWUP = "AWAIT_FOLLOWUP"
    DONE = "DONE"
    NEED_USER_INPUT = "NEED_USER_INPUT"
    PREFLIGHT_BLOCKED = "PREFLIGHT_BLOCKED"
    JOB_FAILED = "JOB_FAILED"


class RunStatus(str, Enum):
    RUNNING = "running"
    SUSPENDED = "suspended"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    EXPIRED = "expired"


class ToolName(str, Enum):
    INSPECT_UPLOADED_INPUTS = "inspect_uploaded_inputs"
    GET_ANALYSIS_SPEC = "get_analysis_spec"
    RUN_PREFLIGHT = "run_preflight"
    SUBMIT_APPROVED_PLAN = "submit_approved_plan"
    GET_JOBS_STATUS = "get_jobs_status"
    QUERY_RESULT_EVIDENCE = "query_result_evidence"


class VerifierDecision(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"


class RouteDecision(ContractModel):
    intent: RouteIntent
    target_profile: RouteTargetProfile
    reason: str = Field(min_length=1)


class Feasibility(ContractModel):
    verdict: FeasibilityVerdict
    reasons: list[str]
    missing_information: list[str]


class AgentDecision(ContractModel):
    action: AgentAction
    reasoning_summary: str = Field(min_length=1)
    feasibility: Feasibility | None = None
    analysis_recommendations: list[AnalysisType]
    requires_approval: bool
    requested_params: dict[str, Any]


class AnalysisCapability(ContractModel):
    analysis_type: AnalysisType
    display_label: str = Field(min_length=1)
    required_inputs: list[str]


class ModelContext(ContractModel):
    """允许发送给模型的最小上下文；禁止传递句柄、凭据与原始文件内容。"""

    user_message: str = Field(min_length=1, max_length=4000)
    active_profile: ActiveProfile
    state: AgentState
    in_scope_job_ids: list[str] = Field(max_length=20)
    conversation_summary: str | None = Field(default=None, max_length=4000)
    available_input_roles: list[str] = Field(default_factory=list, max_length=20)
    analysis_capabilities: list[AnalysisCapability] = Field(default_factory=list, max_length=3)
    available_tools: list[ToolName] = Field(max_length=6)


class Citation(ContractModel):
    artifact: str = Field(min_length=1)
    checksum: str = Field(min_length=1)
    row_ids: list[int]


class RunFocus(ContractModel):
    in_scope_job_ids: list[str]
    resolved_entities: dict[str, str]
    last_citation: Citation | None


class RunState(ContractModel):
    run_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    active_profile: ActiveProfile
    state: AgentState
    step_no: int = Field(ge=0)
    plan_id: str | None
    plan_hash: str | None
    pending_approval_id: str | None
    focus: RunFocus
    model_calls: int = Field(ge=0)
    tool_calls: int = Field(ge=0)
    status: RunStatus
    version: int = Field(ge=0)


class ApprovalRecord(ContractModel):
    approval_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    plan_hash: str = Field(min_length=1)
    status: ApprovalStatus
    expires_at: datetime


class PlanRecord(ContractModel):
    plan_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    analysis_type: AnalysisType
    source_job_id: str = Field(min_length=1)
    requested_params: dict[str, Any]
    effective_params: dict[str, Any]
    contrasts: list[dict[str, Any]]
    plan_hash: str = Field(min_length=1)
    approval_id: str | None
    submitted_job_ids: list[str] = Field(default_factory=list)
    idempotency_key: str | None = None


class ToolResult(ContractModel):
    tool: ToolName
    ok: bool
    rows: list[dict[str, Any]]
    truncated: bool
    row_count: int = Field(ge=0)
    artifact: str | None
    checksum: str | None
    filters: dict[str, Any]
    sort: str | None
    error_code: str | None


class GroundedClaim(ContractModel):
    text: str = Field(min_length=1)
    citation: Citation


class GroundedAnswer(ContractModel):
    claims: list[GroundedClaim]


class VerifierCheck(ContractModel):
    claim_index: int = Field(ge=0)
    number_matches_evidence: bool
    citation_valid: bool
    beyond_evidence: bool
    issues: list[str]


class VerifierVerdict(ContractModel):
    verdict: VerifierDecision
    checks: list[VerifierCheck]


class AgentEvent(ContractModel):
    event_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    step_no: int = Field(ge=0)
    event_type: str = Field(min_length=1)
    payload: dict[str, Any]


class EvalCategory(str, Enum):
    ROUTER = "router"
    RECOMMENDATION = "recommendation"
    CONTRAST = "contrast"
    FAILURE = "failure"
    GROUNDING = "grounding"


class EvalAssemblyName(str, Enum):
    UNIT = "unit"
    OFFLINE = "offline"
    PRODUCTION = "production"


class EvalCaseStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


class GoldenCase(ContractModel):
    case_id: str = Field(min_length=1)
    category: EvalCategory
    adversarial: bool = False
    input: dict[str, Any]
    expected: dict[str, Any]


class EvalCaseResult(ContractModel):
    case_id: str = Field(min_length=1)
    category: EvalCategory
    adversarial: bool
    status: EvalCaseStatus
    duration_ms: float = Field(ge=0)
    model_calls: int = Field(ge=0)
    schema_valid: bool | None = None
    issues: list[str]


class EvalSummary(ContractModel):
    total: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    skipped: int = Field(ge=0)
    pass_rate: float = Field(ge=0, le=1)
    model_calls: int = Field(ge=0)
    p95_latency_ms: float = Field(ge=0)
    metrics: dict[str, float | None]


class EvalRunReport(ContractModel):
    run_id: str = Field(min_length=1)
    assembly: EvalAssemblyName
    model_label: str = Field(min_length=1)
    generated_at: datetime
    skip_reason: str | None
    case_results: list[EvalCaseResult]
    summary: EvalSummary


class EvalDiffReport(ContractModel):
    baseline_run_id: str = Field(min_length=1)
    candidate_run_id: str = Field(min_length=1)
    pass_rate_delta: float
    model_calls_delta: int
    p95_latency_ms_delta: float
    newly_failed: list[str]
    newly_passed: list[str]
    newly_skipped: list[str]
    metric_deltas: dict[str, float | None]
