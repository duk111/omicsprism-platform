from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..models import AnalysisType, JobStatus

class ContractModel(BaseModel):
    """所有 agent 契约默认拒绝未声明字段。"""

    model_config = ConfigDict(extra="forbid")
class AgentThreadStatus(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"

class AgentTurnStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

class AgentMessageRole(str, Enum):
    USER = "user"
    ASSISTANT = "assistant"

class AgentInputBundleStatus(str, Enum):
    ACTIVE = "active"
    CONSUMED = "consumed"
    EXPIRED = "expired"

class ToolName(str, Enum):
    """Internal result contracts; graph actions define the Agent-visible surface."""

    DESCRIBE_METADATA = "describe_metadata"
    ENUMERATE_CONTRASTS = "enumerate_contrasts"
    LIST_JOBS = "list_jobs"
    DESCRIBE_ARTIFACTS = "describe_artifacts"
    QUERY_ARTIFACT = "query_artifact"
    GET_JOBS_STATUS = "get_jobs_status"
    QUERY_RESULT_EVIDENCE = "query_result_evidence"

class VerifierDecision(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"
BriefModelText = Annotated[str, Field(min_length=1, max_length=240)]
AgentParamText = Annotated[str, Field(max_length=200)]
AgentParamValue = AgentParamText | int | float | bool | None
class InputValueCount(ContractModel):
    value: str = Field(max_length=200)
    count: int = Field(ge=0)

class InputGroupLevels(ContractModel):
    column: str = Field(min_length=1, max_length=200)
    values: list[InputValueCount] = Field(max_length=12)

InputRawCell = Annotated[str, Field(max_length=60)]
InputRawRow = Annotated[list[InputRawCell], Field(max_length=10)]
InputFeatureId = Annotated[str, Field(max_length=48)]

class InputInspectionSummary(ContractModel):
    field: str = Field(min_length=1, max_length=50)
    columns: list[str] = Field(max_length=12)
    column_count: int = Field(ge=0)
    row_count: int = Field(ge=0)
    dtype: str | None = Field(default=None, max_length=30)
    group_levels: list[InputGroupLevels] = Field(default_factory=list, max_length=20)
    feature_id_sample: list[InputFeatureId] = Field(default_factory=list, max_length=15)
    feature_id_total: int = Field(default=0, ge=0)
    raw_rows: list[InputRawRow] | None = Field(default=None, max_length=60)
class Citation(ContractModel):
    artifact: str = Field(min_length=1, max_length=500)
    checksum: str = Field(min_length=1, max_length=200)
    row_ids: list[int] = Field(max_length=50)

class RunFocus(ContractModel):
    # 参数记忆的类型门只有在赋值时校验才有意义：否则脏值写得进去、
    # 下一 turn 读库时才在一个与写入点无关的地方抛 ValidationError。
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    in_scope_job_ids: list[str]
    resolved_entities: dict[str, str]
    last_citation: Citation | None
    # 参数记忆限定在「输入来源 + 分析类型」作用域内：换数据或换分析都要重谈。
    draft_params: dict[str, AgentParamValue] = Field(default_factory=dict, max_length=32)
    preferences: dict[str, AgentParamValue] = Field(default_factory=dict, max_length=32)
    draft_analysis_type: str | None = None
    params_source_ref: str | None = None

class RunState(ContractModel):
    run_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    focus: RunFocus
    version: int = Field(ge=0)

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

class AgentInterpretationAnswerDecision(ContractModel):
    """Evidence-backed interpretation response kept for the result contract."""

    action: Literal["answer"]
    reasoning_summary: BriefModelText
    feasibility: None = None
    analysis_recommendations: list[AnalysisType] = Field(max_length=0)
    requires_approval: Literal[False]
    requested_params: dict[str, AgentParamValue] = Field(max_length=0)
    grounded_answer: GroundedAnswer
    advisory_answer: None = None

class AgentTextBlock(ContractModel):
    type: Literal["text"] = "text"
    text: str = Field(min_length=1, max_length=4000)

class AgentAdvisoryBlock(ContractModel):
    type: Literal["advisory"] = "advisory"
    category: Literal["general_biology", "analysis_guidance"]
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
    | AgentJobBlock
    | AgentEvidenceBlock
    | AgentErrorBlock,
    Field(discriminator="type"),
]

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
    focus: RunFocus
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
    trace_id: str = Field(default="trace-local", min_length=1, max_length=200)
    idempotency_key: str = Field(min_length=1, max_length=200)
    request_hash: str = Field(min_length=1)
    status: AgentTurnStatus
    attempt: int = Field(ge=0)
    error_code: str | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    completed_at: datetime | None

class AgentTurnResponse(ContractModel):
    turn_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    trace_id: str = Field(default="trace-local", min_length=1, max_length=200)
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


class AgentJobWaitResponse(ContractModel):
    """Public projection of an Agent wait and its owned analysis Job."""

    wait_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    turn_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    job_id: str = Field(min_length=1)
    wait_status: Literal[
        "waiting",
        "resume_queued",
        "completed",
        "failed",
        "cancelled",
        "expired",
    ]
    job_status: JobStatus | None = None
    progress: int | None = Field(default=None, ge=0, le=100)
    progress_step: str | None = Field(default=None, max_length=200)
    error: str | None = Field(default=None, max_length=1000)
    continuation_turn_id: str | None = None
    created_at: datetime
    updated_at: datetime
    job_updated_at: datetime | None = None


class AgentJobWaitListResponse(ContractModel):
    waits: list[AgentJobWaitResponse]
    next_cursor: str | None = None

class AgentMessageRecord(ContractModel):
    message_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    trace_id: str = Field(default="trace-local", min_length=1, max_length=200)
    user_id: str = Field(min_length=1)
    role: AgentMessageRole
    blocks: list[AgentMessageBlock] = Field(min_length=1, max_length=20)
    created_at: datetime

class AgentMessageResponse(ContractModel):
    message_id: str = Field(min_length=1)
    thread_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    trace_id: str = Field(default="trace-local", min_length=1, max_length=200)
    role: AgentMessageRole
    blocks: list[AgentMessageBlock] = Field(min_length=1, max_length=20)
    created_at: datetime

class AgentMessageListResponse(ContractModel):
    messages: list[AgentMessageResponse]
    next_cursor: str | None = None


class AgentFeedbackRating(str, Enum):
    HELPFUL = "helpful"
    UNHELPFUL = "unhelpful"


class AgentFeedbackCategory(str, Enum):
    INCORRECT_RESULT = "incorrect_result"
    MISSING_CONTEXT = "missing_context"
    BAD_PLAN = "bad_plan"
    UNSAFE_ACTION = "unsafe_action"
    LATENCY = "latency"
    OTHER = "other"


class AgentFeedbackCreateRequest(ContractModel):
    rating: AgentFeedbackRating
    failure_category: AgentFeedbackCategory | None = None
    correction_text: str | None = Field(default=None, min_length=1, max_length=1200)

    @field_validator("correction_text")
    @classmethod
    def _normalize_correction_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return value.strip() or None

    @model_validator(mode="after")
    def _feedback_shape(self) -> "AgentFeedbackCreateRequest":
        if self.rating is AgentFeedbackRating.UNHELPFUL and self.failure_category is None:
            raise ValueError("unhelpful feedback requires a failure_category")
        if self.rating is AgentFeedbackRating.HELPFUL and self.failure_category is not None:
            raise ValueError("helpful feedback cannot include a failure_category")
        return self


class AgentFeedbackRecord(ContractModel):
    feedback_id: str = Field(min_length=1, max_length=200)
    thread_id: str = Field(min_length=1, max_length=200)
    turn_id: str = Field(min_length=1, max_length=200)
    message_id: str = Field(min_length=1, max_length=200)
    trace_id: str = Field(min_length=1, max_length=200)
    user_id: str = Field(min_length=1, max_length=200)
    rating: AgentFeedbackRating
    failure_category: AgentFeedbackCategory | None = None
    correction_text: str | None = Field(default=None, min_length=1, max_length=1200)
    created_at: datetime
    updated_at: datetime


class AgentFeedbackResponse(ContractModel):
    feedback_id: str = Field(min_length=1, max_length=200)
    message_id: str = Field(min_length=1, max_length=200)
    rating: AgentFeedbackRating
    failure_category: AgentFeedbackCategory | None = None
    correction_text: str | None = Field(default=None, min_length=1, max_length=1200)
    created_at: datetime
    updated_at: datetime


class AgentFeedbackListResponse(ContractModel):
    feedback: list[AgentFeedbackResponse]
    next_cursor: str | None = None


class AgentEvalCandidateStatus(str, Enum):
    PENDING_REVIEW = "pending_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


class AgentEvalTraceSummary(ContractModel):
    event_types: list[str] = Field(default_factory=list, max_length=12)
    model_calls: int = Field(default=0, ge=0)
    tool_calls: int = Field(default=0, ge=0)
    total_latency_ms: float = Field(default=0, ge=0)
    error_codes: list[str] = Field(default_factory=list, max_length=12)


class AgentEvalCandidateRecord(ContractModel):
    """Internal, redacted candidate that requires separate human review."""

    candidate_id: str = Field(min_length=1, max_length=200)
    feedback_id: str = Field(min_length=1, max_length=200)
    thread_id: str = Field(min_length=1, max_length=200)
    turn_id: str = Field(min_length=1, max_length=200)
    message_id: str = Field(min_length=1, max_length=200)
    trace_id: str = Field(min_length=1, max_length=200)
    user_id: str = Field(min_length=1, max_length=200)
    status: AgentEvalCandidateStatus = AgentEvalCandidateStatus.PENDING_REVIEW
    rating: AgentFeedbackRating
    failure_category: AgentFeedbackCategory | None = None
    user_message_summary: str = Field(min_length=1, max_length=1200)
    assistant_message_summary: str = Field(min_length=1, max_length=1200)
    correction_summary: str | None = Field(default=None, min_length=1, max_length=1200)
    trace_summary: AgentEvalTraceSummary
    created_at: datetime
    updated_at: datetime


class AgentEvalCandidateExport(ContractModel):
    """Redacted review payload; it intentionally omits user and source ids."""

    candidate_id: str = Field(min_length=1, max_length=200)
    rating: AgentFeedbackRating
    failure_category: AgentFeedbackCategory | None = None
    user_message_summary: str = Field(min_length=1, max_length=1200)
    assistant_message_summary: str = Field(min_length=1, max_length=1200)
    correction_summary: str | None = Field(default=None, min_length=1, max_length=1200)
    trace_summary: AgentEvalTraceSummary
    created_at: datetime


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

class VerifierCheck(ContractModel):
    claim_index: int = Field(ge=0)
    number_matches_evidence: bool
    citation_valid: bool
    beyond_evidence: bool
    issues: list[str]

class VerifierVerdict(ContractModel):
    verdict: VerifierDecision
    checks: list[VerifierCheck]

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
    # router 用例同时记录两条路径，便于报告和 diff 分辨模型回退影响。
    router_rule_passed: bool | None = None
    router_model_passed: bool | None = None

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
