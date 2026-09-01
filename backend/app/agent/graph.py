from __future__ import annotations

from copy import deepcopy
from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .dataset_profile import DatasetProfile
from .param_resolver import (
    AnalysisParams,
    AnalysisProposal,
    ContrastSpec,
    ResolvedRequest,
    ScopeSpec,
)
from .schemas import (
    AgentJobWaitResponse,
    AgentMessageResponse,
    AgentTurnResponse,
    GroundedAnswer,
    RunFocus,
    ToolName,
    ToolResult,
)
from .validation import ContrastPreview, DatasetRef, Issue, ValidationReport
from .context import (
    ConversationMemory,
    ContextAssembler,
    DecisionLedger,
    FactIndex,
    MainModelContext,
    RecentMessages,
    WorkingSet,
)


def _main_output_schema(schema: dict[str, Any]) -> None:
    """Encode the cross-field answer requirement for structured model output."""

    actions = [
        "inspect_dataset",
        "run_analysis",
        "query_result",
        "get_job",
        "ask_user",
        "tool_call",
        "grounded_answer",
        "propose_plan",
    ]
    decision_definition = deepcopy(schema["$defs"]["AgentDecision"])
    answer_decision = {
        "allOf": [
            decision_definition,
            {
                "properties": {"action": {"const": "answer"}},
                "required": ["action"],
            },
        ]
    }
    non_answer_decision = {
        "allOf": [
            decision_definition,
            {
                "properties": {"action": {"enum": actions}},
                "required": ["action"],
            },
        ]
    }
    schema["oneOf"] = [
        {
            "properties": {
                "decision": answer_decision,
                "answer": {"type": "string", "minLength": 1, "maxLength": 1200},
            },
            "required": ["decision", "answer"],
        },
        {
            "properties": {
                "decision": non_answer_decision,
                "answer": {"type": "null"},
            },
            "required": ["decision", "answer"],
        },
    ]


AnalysisTypeName = Literal["DEG", "DEM", "GMA"]
ProvenanceSource = Literal[
    "user_explicit",
    "tool_derived",
    "system_default",
    "user_confirmed",
]


class NodeCapabilityError(ValueError):
    """A semantic node received an action outside its capability whitelist."""


class PlanVersionConflict(ValueError):
    """A confirmation resume references a plan other than the current one."""


class StepBudget(BaseModel):
    """Independent model, tool, and token budgets for one graph turn."""

    model_config = ConfigDict(extra="forbid")

    max_model_steps: int = Field(default=8, ge=1, le=32)
    max_tool_calls: int = Field(default=12, ge=1, le=64)
    max_tokens: int = Field(default=4096, ge=1, le=32768)
    used_model_steps: int = Field(default=0, ge=0)
    used_tool_calls: int = Field(default=0, ge=0)
    # These counters contain only values explicitly reported by the provider.
    # Unknown usage remains unknown rather than being estimated.
    used_prompt_tokens: int = Field(default=0, ge=0)
    used_completion_tokens: int = Field(default=0, ge=0)
    used_tokens: int = Field(default=0, ge=0)
    unknown_usage_model_calls: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _used_counters_within_budget(self) -> "StepBudget":
        if self.used_model_steps > self.max_model_steps:
            raise ValueError("used_model_steps cannot exceed max_model_steps")
        if self.used_tool_calls > self.max_tool_calls:
            raise ValueError("used_tool_calls cannot exceed max_tool_calls")
        if self.used_tokens > self.max_tokens:
            raise ValueError("used_tokens cannot exceed max_tokens")
        return self


class DatasetProfileRef(BaseModel):
    """Ownership-bound reference to a bounded DatasetProfile."""

    model_config = ConfigDict(extra="forbid")

    dataset_id: str = Field(min_length=1, max_length=200)
    owner_id: str = Field(min_length=1, max_length=200)
    filename: str = Field(min_length=1, max_length=500)
    checksum: str = Field(pattern=r"^sha256:[0-9a-fA-F]{64}$")
    profile: DatasetProfile


class JobRef(BaseModel):
    """Ownership-bound job reference kept in graph state."""

    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=1, max_length=200)
    owner_id: str = Field(min_length=1, max_length=200)


class ResultQuerySpec(BaseModel):
    """Bounded candidate query that must be checked against a real Job."""

    model_config = ConfigDict(extra="forbid")

    artifact: str = Field(min_length=1, max_length=500)
    field_path: str | None = Field(default=None, max_length=200)
    filters: dict[str, str | int | float | bool | None] = Field(
        default_factory=dict,
        max_length=8,
    )
    sort: str | None = Field(default=None, max_length=100)
    limit: int | None = Field(default=None, ge=1, le=12)
    resolve_entity: str | None = Field(default=None, max_length=200)


class JobLookupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=200)
    job_id: str = Field(min_length=1, max_length=200)


class JobSummary(BaseModel):
    """Compact ownership-bound status and artifact index for one Job."""

    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=1, max_length=200)
    owner_id: str = Field(min_length=1, max_length=200)
    status: str = Field(min_length=1, max_length=100)
    progress: int | None = Field(default=None, ge=0, le=100)
    progress_step: str | None = Field(default=None, max_length=200)
    error: str | None = Field(default=None, max_length=500)
    artifacts: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(
        default_factory=list,
        max_length=20,
    )


class ResultEvidenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=200)
    job_id: str = Field(min_length=1, max_length=200)
    query: ResultQuerySpec


JobReader = Callable[[JobLookupRequest], JobSummary]
ResultQuerier = Callable[[ResultEvidenceRequest], ToolResult]


class ClarificationItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str = Field(min_length=1, max_length=200)
    options: list[str] = Field(default_factory=list, max_length=20)
    reason: str = Field(min_length=1, max_length=500)


class ClarificationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["clarification"] = "clarification"
    missing: list[ClarificationItem] = Field(default_factory=list, max_length=3)
    question: str = Field(min_length=1, max_length=1000)


class ClarificationResume(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str = Field(min_length=1, max_length=1000)


class StratumSummary(BaseModel):
    """Bounded sample counts shown as part of a pending analysis plan."""

    model_config = ConfigDict(extra="forbid")

    stratum: dict[str, str] = Field(default_factory=dict, max_length=16)
    tested_count: int = Field(default=0, ge=0)
    reference_count: int = Field(default=0, ge=0)
    included: bool = True
    exclusion_reason: str | None = Field(default=None, max_length=300)


class PendingPlan(BaseModel):
    """Versioned, checkpoint-owned plan awaiting a user decision."""

    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(min_length=1, max_length=200)
    plan_version: int = Field(default=1, ge=1)
    thread_id: str = Field(min_length=1, max_length=200)
    analysis_type: AnalysisTypeName
    scope: ScopeSpec
    contrast: ContrastSpec
    params: AnalysisParams
    provenance: dict[str, ProvenanceSource] = Field(default_factory=dict, max_length=64)
    sample_scope: list[StratumSummary] = Field(default_factory=list, max_length=50)
    input_fingerprint: str = Field(pattern=r"^sha256:[0-9a-fA-F]{64}$")
    expires_at: datetime

    @model_validator(mode="after")
    def _params_match_plan(self) -> "PendingPlan":
        if self.params.analysis_type != self.analysis_type:
            raise ValueError("pending plan analysis_type must match params")
        if not hasattr(self.params, "contrast"):
            raise ValueError("pending plan params must contain a contrast")
        if self.params.contrast != self.contrast:
            raise ValueError("pending plan contrast must match params")
        if self.scope != self.contrast.scope:
            raise ValueError("pending plan scope must match contrast scope")
        return self


class ConfirmationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["confirmation"] = "confirmation"
    analysis_type: AnalysisTypeName
    resolved_params: AnalysisParams
    preview: ContrastPreview | None = None
    warnings: list[Issue] = Field(default_factory=list, max_length=20)
    input_fingerprint: str = Field(pattern=r"^sha256:[0-9a-fA-F]{64}$")
    plan_id: str = Field(min_length=1, max_length=200)
    plan_version: int = Field(ge=1)

    @model_validator(mode="after")
    def _analysis_type_matches_params(self) -> "ConfirmationPayload":
        if self.resolved_params.analysis_type != self.analysis_type:
            raise ValueError("analysis_type must match resolved_params")
        return self


class ConfirmationResume(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str = Field(min_length=1, max_length=200)
    plan_version: int = Field(ge=1)
    message: str | None = Field(default=None, min_length=1, max_length=4000)
    approve: bool | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def _resume_semantics(self) -> "ConfirmationResume":
        if self.approve is True:
            if self.message is not None:
                raise ValueError("approved confirmation cannot include a message")
            if self.idempotency_key is None:
                raise ValueError("approved confirmation requires idempotency_key")
        elif self.approve is False:
            if self.message is not None or self.idempotency_key is not None:
                raise ValueError("rejected confirmation cannot include message or idempotency_key")
        elif self.message is None:
            raise ValueError("confirmation message is required unless approve is set")
        elif self.idempotency_key is not None:
            raise ValueError("confirmation message cannot include idempotency_key")
        return self


PendingInterrupt = Annotated[
    ClarificationPayload | ConfirmationPayload,
    Field(discriminator="kind"),
]


class GraphInterrupt(BaseModel):
    """Public resumable interrupt paired with its LangGraph interrupt id."""

    model_config = ConfigDict(extra="forbid")

    interrupt_id: str = Field(min_length=1, max_length=200)
    payload: PendingInterrupt


class GraphPendingInterrupt(BaseModel):
    """Pending graph input together with the turn that must be resumed."""

    model_config = ConfigDict(extra="forbid")

    checkpoint_turn_id: str = Field(min_length=1, max_length=200)
    interrupt: GraphInterrupt


class AgentStreamEvent(BaseModel):
    """Public SSE event carrying durable turn, message, or HITL state."""

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=500)
    event_type: Literal[
        "turn.updated",
        "message.created",
        "job.updated",
        "interrupt.updated",
    ]
    data: AgentTurnResponse | AgentMessageResponse | AgentJobWaitResponse | GraphPendingInterrupt | None


class GraphClarificationResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["clarification"] = "clarification"
    interrupt_id: str = Field(min_length=1, max_length=200)
    answer: str = Field(min_length=1, max_length=1000)


class GraphConfirmationResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["confirmation"] = "confirmation"
    interrupt_id: str = Field(min_length=1, max_length=200)
    plan_id: str = Field(min_length=1, max_length=200)
    plan_version: int = Field(ge=1)
    message: str | None = Field(default=None, min_length=1, max_length=4000)
    approve: bool | None = None

    @model_validator(mode="after")
    def _resume_semantics(self) -> "GraphConfirmationResumeRequest":
        if self.approve is True and self.message is not None:
            raise ValueError("approved confirmation cannot include a message")
        if self.approve is False and self.message is not None:
            raise ValueError("rejected confirmation cannot include a message")
        if self.approve is None and self.message is None:
            raise ValueError("confirmation message is required unless approve is set")
        return self


GraphResumeRequest = Annotated[
    GraphClarificationResumeRequest | GraphConfirmationResumeRequest,
    Field(discriminator="kind"),
]


class GraphTurnResult(BaseModel):
    """Public result of a queued graph invoke or resume operation."""

    model_config = ConfigDict(extra="forbid")

    checkpoint_turn_id: str = Field(min_length=1, max_length=200)
    turn: AgentTurnResponse
    message: AgentMessageResponse | None = None
    interrupt: GraphInterrupt | None = None


class AgentDecision(BaseModel):
    """Single semantic dispatch decision produced by the model boundary."""

    model_config = ConfigDict(extra="forbid")

    action: Literal[
        "answer",
        "inspect_dataset",
        "run_analysis",
        "query_result",
        "get_job",
        "ask_user",
        "tool_call",
        "grounded_answer",
        "propose_plan",
    ]
    analysis_type: AnalysisTypeName | None = None
    proposal: AnalysisProposal | None = None
    job_id: str | None = Field(default=None, max_length=200)
    result_query: ResultQuerySpec | None = None
    question: str | None = Field(default=None, max_length=1000)
    decision_note: str | None = Field(default=None, max_length=240)
    grounded_answer: GroundedAnswer | None = None
    tool: ToolName | None = None
    arguments: dict[str, Any] = Field(default_factory=dict, max_length=16)

    @model_validator(mode="after")
    def _result_query_matches_action(self) -> "AgentDecision":
        if self.action == "query_result" and self.result_query is None:
            raise ValueError("query_result action requires result_query")
        if self.action != "query_result" and self.result_query is not None:
            raise ValueError("result_query is only valid for query_result")
        if self.action == "grounded_answer" and self.grounded_answer is None:
            raise ValueError("grounded_answer action requires a grounded draft")
        if self.action not in {"query_result", "grounded_answer"} and self.grounded_answer is not None:
            raise ValueError("grounded_answer is only valid for result responses")
        allowed_tools = {
            ToolName.DESCRIBE_METADATA,
            ToolName.ENUMERATE_CONTRASTS,
            ToolName.LIST_JOBS,
            ToolName.DESCRIBE_ARTIFACTS,
            ToolName.QUERY_ARTIFACT,
        }
        if self.action == "tool_call":
            if self.tool not in allowed_tools:
                raise ValueError("tool_call action requires a read-only tool")
        elif self.tool is not None or self.arguments:
            raise ValueError("tool and arguments are only valid for tool_call")
        return self


class ToolCallRequest(BaseModel):
    """Typed request passed from the model boundary to a read-only executor."""

    model_config = ConfigDict(extra="forbid")

    tool: ToolName
    arguments: dict[str, Any] = Field(default_factory=dict, max_length=16)


class ToolObservation(BaseModel):
    """Bounded serialized tool output retained for the next model step."""

    model_config = ConfigDict(extra="forbid")

    tool: ToolName
    summary: str = Field(min_length=1, max_length=4000)


class MainModelOutput(BaseModel):
    """Validated model response for Main routing and general answers."""

    model_config = ConfigDict(extra="forbid")

    decision: AgentDecision
    answer: str | None = Field(default=None, min_length=1, max_length=1200)

    @classmethod
    def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
        schema = super().model_json_schema(*args, **kwargs)
        _main_output_schema(schema)
        return schema

    @model_validator(mode="after")
    def _answer_matches_action(self) -> "MainModelOutput":
        if self.decision.action == "answer" and self.answer is None:
            raise ValueError("answer action requires answer text")
        if self.decision.action != "answer" and self.answer is not None:
            raise ValueError("answer text is only valid for answer action")
        if self.decision.action == "ask_user" and not self.decision.question:
            raise ValueError("ask_user action requires a question")
        return self


MainDecisionModel = Callable[[MainModelContext], object]
ToolExecutor = Callable[[ToolCallRequest, "GraphState"], object]


class DatasetLoadRequest(BaseModel):
    """Ownership-scoped request for validation inputs; it carries no file bytes."""

    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=200)
    dataset_ids: list[str] = Field(default_factory=list, max_length=6)


DatasetLoader = Callable[[DatasetLoadRequest], list[DatasetRef]]


class AnalysisExecutionRequest(BaseModel):
    """Validated analysis submission request with an ownership and idempotency scope."""

    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=200)
    thread_id: str = Field(min_length=1, max_length=200)
    trace_id: str = Field(default="trace-local", min_length=1, max_length=200)
    turn_id: str = Field(default="turn-local", min_length=1, max_length=200)
    run_id: str = Field(default="run-local", min_length=1, max_length=200)
    dataset_ids: list[str] = Field(min_length=1, max_length=6)
    resolved_params: AnalysisParams
    input_fingerprint: str = Field(pattern=r"^sha256:[0-9a-fA-F]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=200)
    # Fixed scopes carry materialized inputs only across the submission boundary;
    # raw bytes must never be persisted in graph state or checkpoints.
    scoped_inputs: list[DatasetRef] = Field(default_factory=list, max_length=6, exclude=True)


JobSubmitter = Callable[[AnalysisExecutionRequest], JobRef]


class GraphState(BaseModel):
    """Compact state shared by the v3 semantic graph nodes."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    thread_id: str = Field(min_length=1, max_length=200)
    user_id: str = Field(min_length=1, max_length=200)
    trace_id: str = Field(default="trace-local", min_length=1, max_length=200)
    turn_id: str = Field(default="turn-local", min_length=1, max_length=200)
    run_id: str = Field(default="run-local", min_length=1, max_length=200)
    user_message: str = Field(min_length=1, max_length=4000)
    focus: RunFocus = Field(default_factory=lambda: RunFocus(
        in_scope_job_ids=[],
        resolved_entities={},
        last_citation=None,
    ))
    version: int = Field(default=0, ge=0)
    conversation_summary: str | None = Field(default=None, max_length=4000)
    recent_messages: RecentMessages = Field(default_factory=lambda: RecentMessages(
        context_version="messages.v1:empty"
    ))
    conversation_memory: ConversationMemory = Field(default_factory=lambda: ConversationMemory(
        context_version="memory.v1:empty"
    ))
    active_input_bundle_id: str | None = Field(default=None, min_length=1, max_length=200)
    dataset_profiles: list[DatasetProfileRef] = Field(default_factory=list, max_length=6)
    current_job: JobRef | None = None
    recent_jobs: list[JobRef] = Field(default_factory=list, max_length=20)
    decision: AgentDecision | None = None
    response_text: str | None = Field(default=None, max_length=1200)
    clarification_answer: str | None = Field(default=None, max_length=1000)
    resolved_request: ResolvedRequest | None = None
    validation_report: ValidationReport | None = None
    job_summary: JobSummary | None = None
    grounded_answer: GroundedAnswer | None = None
    pending_plan: PendingPlan | None = None
    confirmed_params: AnalysisParams | None = None
    tool_observations: list[ToolObservation] = Field(default_factory=list, max_length=12)
    pending_interrupt: PendingInterrupt | None = None
    step_budget: StepBudget = Field(default_factory=StepBudget)

    @model_validator(mode="after")
    def _references_belong_to_user(self) -> "GraphState":
        references = [*self.dataset_profiles, *self.recent_jobs]
        if self.current_job is not None:
            references.append(self.current_job)
        if self.job_summary is not None:
            references.append(self.job_summary)
        if any(reference.owner_id != self.user_id for reference in references):
            raise ValueError("graph references must belong to user_id")
        if self.pending_plan is not None and self.pending_plan.thread_id != self.thread_id:
            raise ValueError("pending plan must belong to thread_id")
        return self


def build_agent_graph(
    model: MainDecisionModel,
    dataset_loader: DatasetLoader,
    job_submitter: JobSubmitter,
    job_reader: JobReader,
    result_querier: ResultQuerier,
    *,
    checkpointer: object | None = None,
    tool_executor: ToolExecutor | None = None,
    trace_recorder: object | None = None,
):
    """Compile the v3 graph around injected model and deterministic data boundaries."""

    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph

    from .nodes.analysis import analysis_node
    from .nodes.main import main_node, route_after_main
    from .nodes.result_qa import result_qa_node

    builder = StateGraph(GraphState)
    builder.add_node("main", main_node(model, tool_executor, trace_recorder))
    builder.add_node(
        "analysis",
        analysis_node(dataset_loader, job_submitter),
        destinations=("analysis", END),
    )
    builder.add_node("result_qa", result_qa_node(job_reader, result_querier))
    builder.add_edge(START, "main")
    builder.add_conditional_edges(
        "main",
        route_after_main,
        {"analysis": "analysis", "result_qa": "result_qa", "end": END},
    )
    builder.add_edge("result_qa", END)
    graph_checkpointer = checkpointer if checkpointer is not None else InMemorySaver()
    return builder.compile(checkpointer=graph_checkpointer)
