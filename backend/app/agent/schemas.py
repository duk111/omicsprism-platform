from __future__ import annotations

from datetime import datetime
from enum import Enum
import json
import re
from typing import Annotated, Any, Literal
from typing_extensions import TypeAliasType

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..models import AnalysisType, JobStatus


class ContractModel(BaseModel):
    """所有 agent 契约默认拒绝未声明字段。"""

    model_config = ConfigDict(extra="forbid")


class RouteIntent(str, Enum):
    HELP = "help"
    EXPLAIN_PLAN = "explain_plan"
    ANALYZE = "analyze"
    INTERPRET = "interpret"
    RERUN = "rerun"
    DESCRIBE_ONLY = "describe_only"
    CHECK_STATUS = "check_status"
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
    CALL_TOOL = "call_tool"
    PROPOSE_PLAN = "propose_plan"
    REQUEST_MORE_DATA = "request_more_data"
    REQUEST_APPROVAL = "request_approval"
    ANSWER = "answer"


class FeasibilityVerdict(str, Enum):
    ANSWERABLE = "answerable"
    ANSWERABLE_WITH_CAVEATS = "answerable_with_caveats"
    NOT_ANSWERABLE = "not_answerable"


class AgentState(str, Enum):
    COLLECT_INTENT = "COLLECT_INTENT"
    ADVISE = "ADVISE"
    CHECK_INPUTS = "CHECK_INPUTS"
    # 仅 fixture 协调器使用；生产协调器直接生成审批进入 WAIT_EXECUTION_CONFIRMATION。
    WAIT_PLAN_CONFIRMATION = "WAIT_PLAN_CONFIRMATION"
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
    """Internal result contracts; graph actions define the Agent-visible surface."""

    GET_JOBS_STATUS = "get_jobs_status"
    QUERY_RESULT_EVIDENCE = "query_result_evidence"


class VerifierDecision(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"


class RouteDecision(ContractModel):
    intent: RouteIntent
    target_profile: RouteTargetProfile
    reason: str = Field(min_length=1)
    # 显式的「本条消息在补充/修改分析参数」信号。reason 只是给人看的说明，
    # 不能用于控制流：改一句文案就会改变行为。待批期是否作废旧计划、
    # 是否清空 draft_params 都以这个布尔为准。
    is_param_negotiation: bool = False


class ModelRouteDecision(ContractModel):
    """模型意图分类契约；不包含工具、参数或副作用字段。"""

    intent: RouteIntent
    target_profile: RouteTargetProfile
    is_param_negotiation: bool
    confidence: Literal["high", "low"]
    reason: Annotated[str, Field(min_length=1, max_length=240)]


BriefModelText = Annotated[str, Field(min_length=1, max_length=240)]
AgentParamText = Annotated[str, Field(max_length=200)]
AgentParamValue = AgentParamText | int | float | bool | None


class ToolParamSet(ContractModel):
    """预检参数的显式字段集合，禁止模型传入自由字典。"""

    compare_field: str | None = Field(default=None, max_length=200)
    tested_levels: str | None = Field(default=None, max_length=200)
    reference_level: str | None = Field(default=None, max_length=200)
    same_fields: str | None = Field(default=None, max_length=200)
    min_replicates: int | None = Field(default=None, ge=1, le=1000)
    padj_cutoff: float | None = Field(default=None, ge=0, le=1)
    log2fc_cutoff: float | None = Field(default=None, ge=0)
    min_total_count: int | None = Field(default=None, ge=0)


class ToolCallArguments(ContractModel):
    """只读工具参数的收窄契约；不接受任意键或表达式。"""

    analysis_type: AnalysisType | None = None
    params: ToolParamSet | None = None
    job_ids: list[str] = Field(default_factory=list, max_length=20)
    job_id: str | None = Field(default=None, max_length=200)
    artifact: str | None = Field(default=None, max_length=500)
    field_path: str | None = Field(default=None, max_length=200)
    filters: dict[str, AgentParamValue] = Field(default_factory=dict, max_length=8)
    sort: str | None = Field(default=None, max_length=100)
    limit: int | None = Field(default=None, ge=1, le=12)
    resolve_entity: str | None = Field(default=None, max_length=200)

JsonValue = TypeAliasType(
    "JsonValue",
    str | int | float | bool | None | list["JsonValue"] | dict[str, "JsonValue"],
)

_ALLOWED_FACT_KEYS = frozenset({
    "situation", "analysis_type", "contrast_count", "expires_in_minutes",
    "user_options", "error_count", "errors", "roles_present", "roles_missing",
    "role_summaries", "analysis_capabilities", "contrasts", "effective_params",
    "old_analysis_type", "new_analysis_type", "changed_param_keys",
    "has_inputs", "has_pending_plan", "alignment", "job_id", "error_text",
    "advice_category",
})


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
    inference_note: str | None = Field(default=None, max_length=200)
    tool: ToolName | None = None
    arguments: ToolCallArguments | None = None


class AgentToolCallDecision(ContractModel):
    action: Literal["call_tool"]
    tool: ToolName
    arguments: ToolCallArguments
    reasoning_summary: BriefModelText
    feasibility: None = None
    analysis_recommendations: list[AnalysisType] = Field(max_length=0)
    requires_approval: Literal[False]
    requested_params: dict[str, AgentParamValue] = Field(max_length=0)
    grounded_answer: None = None
    advisory_answer: None = None


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


class AgentNarrationDecision(ContractModel):
    """系统事实叙述契约；除 narration 外不允许任何副作用字段。"""

    action: Literal["answer"]
    narration: str = Field(min_length=1, max_length=800)
    feasibility: None = None
    analysis_recommendations: list[AnalysisType] = Field(max_length=0)
    requires_approval: Literal[False]
    requested_params: dict[str, AgentParamValue] = Field(max_length=0)
    grounded_answer: None = None
    advisory_answer: None = None


class AnswerableFeasibility(ContractModel):
    verdict: Literal["answerable", "answerable_with_caveats"]
    reasons: list[BriefModelText] = Field(max_length=3)
    missing_information: list[BriefModelText] = Field(max_length=0)


class NotAnswerableFeasibility(ContractModel):
    verdict: Literal["not_answerable"]
    reasons: list[BriefModelText] = Field(max_length=3)
    missing_information: list[BriefModelText] = Field(min_length=1, max_length=3)


class AgentAnalysisPlanDecision(ContractModel):
    action: Literal["propose_plan"]
    reasoning_summary: BriefModelText
    feasibility: AnswerableFeasibility
    analysis_recommendations: list[AnalysisType] = Field(min_length=1, max_length=3)
    requires_approval: Literal[True]
    requested_params: dict[str, AgentParamValue] = Field(max_length=32)
    inference_note: str | None = Field(default=None, max_length=200)
    inference_note: str | None = Field(default=None, max_length=200)
    grounded_answer: None
    advisory_answer: None


class AgentAnalysisClarificationDecision(ContractModel):
    action: Literal["request_more_data"]
    reasoning_summary: BriefModelText
    feasibility: NotAnswerableFeasibility
    analysis_recommendations: list[AnalysisType] = Field(max_length=0)
    requires_approval: Literal[False]
    requested_params: dict[str, AgentParamValue] = Field(max_length=0)
    grounded_answer: None
    advisory_answer: None


AgentAnalysisDecision = Annotated[
    AgentAnalysisPlanDecision | AgentAnalysisClarificationDecision,
    Field(discriminator="action"),
]


class AgentInterpretationQueryDecision(ContractModel):
    """解释 profile 的第一步只能请求受限证据查询。"""

    action: Literal["answer"]
    reasoning_summary: BriefModelText
    feasibility: None = None
    analysis_recommendations: list[AnalysisType] = Field(max_length=0)
    requires_approval: Literal[False]
    requested_params: dict[str, AgentParamValue] = Field(max_length=6)
    grounded_answer: None = None
    advisory_answer: None = None


class AnalysisCapability(ContractModel):
    analysis_type: AnalysisType
    display_label: str = Field(min_length=1)
    required_inputs: list[str]


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


class ModelContext(ContractModel):
    """允许发送给模型的最小上下文；禁止传递句柄、凭据与原始文件内容。"""

    user_message: str = Field(min_length=1, max_length=4000)
    active_profile: ActiveProfile
    state: AgentState
    in_scope_job_ids: list[str] = Field(max_length=20)
    available_result_artifacts: list[str] = Field(default_factory=list, max_length=50)
    conversation_summary: str | None = Field(default=None, max_length=4000)
    available_input_roles: list[str] = Field(default_factory=list, max_length=20)
    input_summaries: list[InputInspectionSummary] = Field(default_factory=list, max_length=6)
    analysis_capabilities: list[AnalysisCapability] = Field(default_factory=list, max_length=3)
    available_tools: list[ToolName] = Field(max_length=6)
    tool_history: list[ToolResult] = Field(default_factory=list, max_length=4, exclude_if=lambda value: not value)
    evidence: ToolResult | None = None
    confirmed_params: dict[str, AgentParamValue] = Field(default_factory=dict, max_length=32)
    # R1：retry_hint 只能由 runtime 用服务端常量模板填充，内容限于
    # 「上一次决策哪里不合法 + 合法取值范围（同一 context 已暴露的 artifact / job id）」。
    # 禁止把用户原文、文件内容、异常堆栈写进去。
    retry_hint: Annotated[str, Field(max_length=300)] | None = None
    system_facts: dict[str, JsonValue] | None = Field(default=None, exclude_if=lambda value: value is None)
    # 仅供服务端选择只读循环响应 schema；不发送给模型作为业务事实。
    allow_tool_calls: bool = Field(default=False, exclude=True)

    @field_validator("system_facts")
    @classmethod
    def _validate_system_facts(cls, value: dict[str, JsonValue] | None) -> dict[str, JsonValue] | None:
        if value is None:
            return None
        if set(value) - _ALLOWED_FACT_KEYS:
            raise ValueError("system_facts contains a disallowed key")

        def walk(item: JsonValue, depth: int = 1) -> None:
            if depth > 3:
                raise ValueError("system_facts nesting depth exceeds 3")
            if isinstance(item, dict):
                for key, child in item.items():
                    if not isinstance(key, str):
                        raise ValueError("system_facts keys must be strings")
                    walk(child, depth + 1)
            elif isinstance(item, list):
                for child in item:
                    walk(child, depth)
            elif isinstance(item, str):
                if len(item) > 400:
                    raise ValueError("system_facts text is too long")
                # 收窄为真正的路径形态，避免误杀斜杠
                if re.search(
                    r"[A-Za-z][A-Za-z0-9+.-]*://|"  # scheme://
                    r"(^|\s)[/~][A-Za-z0-9._-]|"  # 绝对路径
                    r"\.\./|"  # 相对路径
                    r"[A-Za-z]:\\|"  # Windows 路径
                    r"sha256:[0-9a-fA-F]{16,}|"  # checksum
                    r"\b[0-9a-fA-F]{32,}\b|"  # 长 hex
                    r"Traceback \(most recent call last\)",  # 异常堆栈
                    item,
                    re.I
                ):
                    raise ValueError("system_facts contains sensitive data")

        walk(value)
        situation = value.get("situation")
        supported_situations = {
            "pending_approval", "preflight_blocked", "explain_plan", "capability_help",
            "input_receipt", "plan_superseded", "status_not_running", "job_failed",
        }
        if situation not in supported_situations:
            raise ValueError("system_facts does not match a supported situation")
        if situation == "pending_approval":
            contrast_count = value.get("contrast_count")
            if contrast_count is None:
                raise ValueError("pending_approval requires contrast_count")
            if not isinstance(contrast_count, int) or isinstance(contrast_count, bool) or contrast_count < 0:
                raise ValueError("contrast_count must be a non-negative integer")
            expires_in_minutes = value.get("expires_in_minutes")
            if expires_in_minutes is None:
                raise ValueError("pending_approval requires expires_in_minutes")
            if not isinstance(expires_in_minutes, int) or isinstance(expires_in_minutes, bool) or expires_in_minutes < 0:
                raise ValueError("expires_in_minutes must be a non-negative integer")
            user_options = value.get("user_options")
            if user_options != ["approve", "reject", "modify_params", "explain_plan"]:
                raise ValueError("user_options is not the server-defined list")
        elif situation == "preflight_blocked":
            errors = value.get("errors")
            if errors is None:
                raise ValueError("preflight_blocked requires errors")
            roles = value.get("roles_present")
            if roles is None:
                raise ValueError("preflight_blocked requires roles_present")
            error_count = value.get("error_count")
            if error_count is None:
                raise ValueError("preflight_blocked requires error_count")
            if not isinstance(error_count, int) or isinstance(error_count, bool) or error_count < 0:
                raise ValueError("error_count must be a non-negative integer")
            if not isinstance(errors, list) or len(errors) > 3 or any(not isinstance(item, str) or len(item) > 200 for item in errors):
                raise ValueError("errors must contain at most three bounded strings")
            if not isinstance(roles, list) or len(roles) > 20 or any(not isinstance(item, str) or len(item) > 50 for item in roles):
                raise ValueError("roles_present is invalid")
        serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(serialized) > 2048:
            raise ValueError("system_facts exceeds 2 KB")
        return value


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


class AgentInterpretationAnswerDecision(ContractModel):
    """解释 profile 的第二步只能组织当前证据适配器返回的行。"""

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
    inference_note: str | None = Field(default=None, max_length=200)
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


AgentDecision.model_rebuild()
ModelContext.model_rebuild()
AgentTurnExecutionResult.model_rebuild()
