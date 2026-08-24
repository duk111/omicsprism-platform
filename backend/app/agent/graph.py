from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .dataset_profile import DatasetProfile
from .param_resolver import AnalysisParams, AnalysisProposal, ResolvedRequest
from .schemas import (
    AgentMessageResponse,
    AgentTurnResponse,
    GroundedAnswer,
    RunFocus,
    ToolResult,
)
from .validation import ContrastPreview, DatasetRef, Issue, ValidationReport


AnalysisTypeName = Literal["DEG", "DEM", "GMA"]


class NodeCapabilityError(ValueError):
    """A semantic node received an action outside its capability whitelist."""


class StepBudget(BaseModel):
    """Bounded graph execution budget; it is not a workflow state enum."""

    model_config = ConfigDict(extra="forbid")

    max_steps: int = Field(default=8, ge=1, le=32)
    used_steps: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _used_steps_within_budget(self) -> "StepBudget":
        if self.used_steps > self.max_steps:
            raise ValueError("used_steps cannot exceed max_steps")
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


class ConfirmationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["confirmation"] = "confirmation"
    analysis_type: AnalysisTypeName
    resolved_params: AnalysisParams
    preview: ContrastPreview | None = None
    warnings: list[Issue] = Field(default_factory=list, max_length=20)
    input_fingerprint: str = Field(pattern=r"^sha256:[0-9a-fA-F]{64}$")

    @model_validator(mode="after")
    def _analysis_type_matches_params(self) -> "ConfirmationPayload":
        if self.resolved_params.analysis_type != self.analysis_type:
            raise ValueError("analysis_type must match resolved_params")
        return self


class ConfirmationResume(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: Literal["run", "modify", "cancel"]
    modification: str | None = Field(default=None, min_length=1, max_length=1000)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def _required_action_fields(self) -> "ConfirmationResume":
        if self.action == "run" and self.idempotency_key is None:
            raise ValueError("run action requires idempotency_key")
        if self.action == "modify" and self.modification is None:
            raise ValueError("modify action requires modification")
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


class GraphClarificationResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["clarification"] = "clarification"
    interrupt_id: str = Field(min_length=1, max_length=200)
    answer: str = Field(min_length=1, max_length=1000)


class GraphConfirmationResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["confirmation"] = "confirmation"
    interrupt_id: str = Field(min_length=1, max_length=200)
    action: Literal["run", "modify", "cancel"]
    modification: str | None = Field(default=None, min_length=1, max_length=1000)

    @model_validator(mode="after")
    def _modify_requires_text(self) -> "GraphConfirmationResumeRequest":
        if self.action == "modify" and self.modification is None:
            raise ValueError("modify action requires modification")
        if self.action != "modify" and self.modification is not None:
            raise ValueError("modification is only valid for modify action")
        return self


GraphResumeRequest = Annotated[
    GraphClarificationResumeRequest | GraphConfirmationResumeRequest,
    Field(discriminator="kind"),
]


class GraphTurnResult(BaseModel):
    """Public result of an inline graph invoke or resume operation."""

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
    ]
    analysis_type: AnalysisTypeName | None = None
    proposal: AnalysisProposal | None = None
    job_id: str | None = Field(default=None, max_length=200)
    result_query: ResultQuerySpec | None = None
    question: str | None = Field(default=None, max_length=1000)
    decision_note: str | None = Field(default=None, max_length=240)

    @model_validator(mode="after")
    def _result_query_matches_action(self) -> "AgentDecision":
        if self.action == "query_result" and self.result_query is None:
            raise ValueError("query_result action requires result_query")
        if self.action != "query_result" and self.result_query is not None:
            raise ValueError("result_query is only valid for query_result")
        return self


class MainModelContext(BaseModel):
    """Prompt-safe context for the top-level semantic decision."""

    model_config = ConfigDict(extra="forbid")

    user_message: str = Field(min_length=1, max_length=4000)
    conversation_summary: str | None = Field(default=None, max_length=4000)
    dataset_roles: list[str] = Field(default_factory=list, max_length=6)
    current_job_id: str | None = Field(default=None, max_length=200)
    recent_job_ids: list[str] = Field(default_factory=list, max_length=20)


class MainModelOutput(BaseModel):
    """Validated model response for Main routing and general answers."""

    model_config = ConfigDict(extra="forbid")

    decision: AgentDecision
    answer: str | None = Field(default=None, min_length=1, max_length=1200)

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
    dataset_ids: list[str] = Field(min_length=1, max_length=6)
    resolved_params: AnalysisParams
    input_fingerprint: str = Field(pattern=r"^sha256:[0-9a-fA-F]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=200)


JobSubmitter = Callable[[AnalysisExecutionRequest], JobRef]


class GraphState(BaseModel):
    """Compact state shared by the v3 semantic graph nodes."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    thread_id: str = Field(min_length=1, max_length=200)
    user_id: str = Field(min_length=1, max_length=200)
    user_message: str = Field(min_length=1, max_length=4000)
    focus: RunFocus = Field(default_factory=lambda: RunFocus(
        in_scope_job_ids=[],
        resolved_entities={},
        last_citation=None,
    ))
    version: int = Field(default=0, ge=0)
    conversation_summary: str | None = Field(default=None, max_length=4000)
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
        return self


def build_agent_graph(
    model: MainDecisionModel,
    dataset_loader: DatasetLoader,
    job_submitter: JobSubmitter,
    job_reader: JobReader,
    result_querier: ResultQuerier,
    *,
    checkpointer: object | None = None,
):
    """Compile the v3 graph around injected model and deterministic data boundaries."""

    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.graph import END, START, StateGraph

    from .nodes.analysis import analysis_node
    from .nodes.main import main_node, route_after_main
    from .nodes.result_qa import result_qa_node

    builder = StateGraph(GraphState)
    builder.add_node("main", main_node(model))
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
