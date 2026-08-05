from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..models import AnalysisType, JobStatus


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


class AdvisoryCategory(str, Enum):
    GENERAL_BIOLOGY = "general_biology"
    ANALYSIS_GUIDANCE = "analysis_guidance"


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
    ADVISE = "ADVISE"
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
    REJECTED = "rejected"
    EXPIRED = "expired"


class AgentThreadStatus(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class AgentTurnStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class AgentMessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"


class AgentInputBundleStatus(str, Enum):
    ACTIVE = "active"
    CONSUMED = "consumed"
    EXPIRED = "expired"


class AgentInputSourceKind(str, Enum):
    EXISTING_JOB = "existing_job"
    STAGED_BUNDLE = "staged_bundle"


class AgentApprovalDecision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"


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


BriefModelText = Annotated[str, Field(min_length=1, max_length=240)]
AgentParamText = Annotated[str, Field(max_length=200)]
AgentParamValue = AgentParamText | int | float | bool | None


class Feasibility(ContractModel):
    verdict: FeasibilityVerdict
    reasons: list[BriefModelText] = Field(max_length=3)
    missing_information: list[BriefModelText] = Field(max_length=3)


class AgentDecision(ContractModel):
    action: AgentAction
    reasoning_summary: BriefModelText
    feasibility: Feasibility | None = None
    analysis_recommendations: list[AnalysisType] = Field(max_length=3)
    requires_approval: bool
    requested_params: dict[str, AgentParamValue] = Field(max_length=32)
    grounded_answer: GroundedAnswer | None = None
    advisory_answer: str | None = Field(default=None, min_length=1, max_length=1200)


class AgentAdvisoryDecision(ContractModel):
    """咨询态的窄模型输出契约；副作用相关字段由 schema 固定为空。"""

    action: Literal["answer"]
    reasoning_summary: BriefModelText
    feasibility: None
    analysis_recommendations: list[AnalysisType] = Field(max_length=0)
    requires_approval: Literal[False]
    requested_params: dict[str, AgentParamValue] = Field(max_length=0)
    grounded_answer: None
    advisory_answer: str = Field(min_length=1, max_length=1200)


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
    evidence: ToolResult | None = None


class Citation(ContractModel):
    artifact: str = Field(min_length=1, max_length=500)
    checksum: str = Field(min_length=1, max_length=200)
    row_ids: list[int] = Field(max_length=50)


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


class AgentInputSourceRef(ContractModel):
    kind: AgentInputSourceKind
    source_id: str = Field(min_length=1, max_length=200)


class PlanRecord(ContractModel):
    plan_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    analysis_type: AnalysisType
    input_source: AgentInputSourceRef
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
    text: str = Field(min_length=1, max_length=1000)
    citation: Citation


class GroundedAnswer(ContractModel):
    claims: list[GroundedClaim] = Field(max_length=50)


class AgentTextBlock(ContractModel):
    type: Literal["text"] = "text"
    text: str = Field(min_length=1, max_length=4000)


class AgentAdvisoryBlock(ContractModel):
    type: Literal["advisory"] = "advisory"
    category: AdvisoryCategory
    text: str = Field(min_length=1, max_length=1200)


class AgentInputFileResponse(ContractModel):
    file_id: str = Field(min_length=1)
    field: str = Field(min_length=1)
    filename: str = Field(min_length=1)
    checksum: str = Field(min_length=1)
    content_type: str | None = None
    size_bytes: int = Field(ge=0)
    created_at: datetime


class AgentInputSummaryBlock(ContractModel):
    type: Literal["input_summary"] = "input_summary"
    bundle_id: str = Field(min_length=1)
    files: list[AgentInputFileResponse] = Field(max_length=6)


class AgentRecommendationItem(ContractModel):
    analysis_type: AnalysisType
    display_label: str = Field(min_length=1, max_length=100)
    reasons: list[BriefModelText] = Field(default_factory=list, max_length=3)


class AgentRecommendationBlock(ContractModel):
    type: Literal["recommendation"] = "recommendation"
    recommendations: list[AgentRecommendationItem] = Field(max_length=3)


class AgentPlanBlock(ContractModel):
    type: Literal["plan"] = "plan"
    plan_id: str = Field(min_length=1)
    plan_hash: str = Field(min_length=1)
    analysis_type: AnalysisType
    requested_params: dict[str, AgentParamValue] = Field(max_length=32)
    effective_params: dict[str, AgentParamValue] = Field(max_length=32)
    contrasts: list[dict[str, Any]] = Field(max_length=50)
    warnings: list[str] = Field(default_factory=list, max_length=20)
    expires_at: datetime


class AgentApprovalBlock(ContractModel):
    type: Literal["approval"] = "approval"
    approval_id: str = Field(min_length=1)
    plan_hash: str = Field(min_length=1)
    status: ApprovalStatus
    expires_at: datetime


class AgentJobBlock(ContractModel):
    type: Literal["job"] = "job"
    job_id: str = Field(min_length=1)
    status: JobStatus
    progress: int = Field(ge=0, le=100)
    progress_url: str = Field(min_length=1)
    results_url: str | None = None


class AgentEvidenceBlock(ContractModel):
    type: Literal["evidence"] = "evidence"
    claims: list[GroundedClaim] = Field(max_length=50)


class AgentErrorBlock(ContractModel):
    type: Literal["error"] = "error"
    code: str = Field(min_length=1, max_length=100)
    user_message: str = Field(min_length=1, max_length=1000)
    retryable: bool
    request_id: str | None = None


AgentMessageBlock = Annotated[
    AgentTextBlock
    | AgentAdvisoryBlock
    | AgentInputSummaryBlock
    | AgentRecommendationBlock
    | AgentPlanBlock
    | AgentApprovalBlock
    | AgentJobBlock
    | AgentEvidenceBlock
    | AgentErrorBlock,
    Field(discriminator="type"),
]


class AgentTurnExecutionResult(ContractModel):
    state: RunState
    blocks: list[AgentMessageBlock] = Field(max_length=20)
    expected_version: int = Field(ge=0)
    events: list[AgentEvent] = Field(default_factory=list, max_length=20)


class AgentThreadCreateRequest(ContractModel):
    focus_job_ids: list[str] = Field(default_factory=list, max_length=20)


class AgentThreadRecord(ContractModel):
    thread_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=200)
    current_run_id: str = Field(min_length=1)
    status: AgentThreadStatus
    version: int = Field(ge=0)
    created_at: datetime
    updated_at: datetime


class AgentThreadResponse(ContractModel):
    thread_id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=200)
    current_run_id: str = Field(min_length=1)
    status: AgentThreadStatus
    version: int = Field(ge=0)
    created_at: datetime
    updated_at: datetime


class AgentRunResponse(ContractModel):
    run_id: str = Field(min_length=1)
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


class AgentThreadDetailResponse(ContractModel):
    thread: AgentThreadResponse
    run: AgentRunResponse


class AgentThreadListResponse(ContractModel):
    threads: list[AgentThreadResponse]
    next_cursor: str | None = None


class AgentTurnCreateRequest(ContractModel):
    message: str = Field(min_length=1, max_length=4000)
    input_bundle_id: str | None = None
    focus_job_ids: list[str] = Field(default_factory=list, max_length=20)


class AgentTurnRecord(ContractModel):
    turn_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=200)
    request_hash: str = Field(min_length=1)
    status: AgentTurnStatus
    attempt: int = Field(ge=0)
    lease_owner: str | None
    lease_expires_at: datetime | None
    error_code: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class AgentTurnResponse(ContractModel):
    turn_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    status: AgentTurnStatus
    attempt: int = Field(ge=0)
    error_code: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class AgentTurnListResponse(ContractModel):
    turns: list[AgentTurnResponse]
    next_cursor: str | None = None


class AgentApprovalRequest(ContractModel):
    decision: AgentApprovalDecision
    plan_hash: str = Field(min_length=1)


class AgentMessageRecord(ContractModel):
    message_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    role: AgentMessageRole
    blocks: list[AgentMessageBlock] = Field(min_length=1, max_length=20)
    created_at: datetime


class AgentMessageResponse(ContractModel):
    message_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    role: AgentMessageRole
    blocks: list[AgentMessageBlock] = Field(min_length=1, max_length=20)
    created_at: datetime


class AgentMessageListResponse(ContractModel):
    messages: list[AgentMessageResponse]
    next_cursor: str | None = None


class AgentInputBundleRecord(ContractModel):
    bundle_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    status: AgentInputBundleStatus
    expires_at: datetime
    created_at: datetime


class AgentInputFileRecord(ContractModel):
    file_id: str = Field(min_length=1)
    bundle_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    field: str = Field(min_length=1)
    filename: str = Field(min_length=1)
    storage_key: str = Field(min_length=1)
    checksum: str = Field(min_length=1)
    content_type: str | None
    size_bytes: int = Field(ge=0)
    created_at: datetime


class AgentInputBundleResponse(ContractModel):
    bundle_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    status: AgentInputBundleStatus
    expires_at: datetime
    created_at: datetime
    files: list[AgentInputFileResponse] = Field(default_factory=list, max_length=6)


class AgentStreamEvent(ContractModel):
    event_id: str = Field(min_length=1)
    event_type: Literal["turn.updated", "message.created"]
    data: AgentTurnResponse | AgentMessageResponse


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


AgentDecision.model_rebuild()
ModelContext.model_rebuild()
AgentTurnExecutionResult.model_rebuild()
