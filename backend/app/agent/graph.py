from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .dataset_profile import DatasetProfile
from .param_resolver import AnalysisParams, AnalysisProposal
from .validation import ContrastPreview


AnalysisTypeName = Literal["DEG", "DEM", "GMA"]


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


class ConfirmationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["confirmation"] = "confirmation"
    analysis_type: AnalysisTypeName
    resolved_params: AnalysisParams
    preview: ContrastPreview | None = None
    input_fingerprint: str = Field(pattern=r"^sha256:[0-9a-fA-F]{64}$")

    @model_validator(mode="after")
    def _analysis_type_matches_params(self) -> "ConfirmationPayload":
        if self.resolved_params.analysis_type != self.analysis_type:
            raise ValueError("analysis_type must match resolved_params")
        return self


PendingInterrupt = Annotated[
    ClarificationPayload | ConfirmationPayload,
    Field(discriminator="kind"),
]


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
    question: str | None = Field(default=None, max_length=1000)
    decision_note: str | None = Field(default=None, max_length=240)


class GraphState(BaseModel):
    """Compact state shared by the v3 semantic graph nodes."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    thread_id: str = Field(min_length=1, max_length=200)
    user_id: str = Field(min_length=1, max_length=200)
    user_message: str = Field(min_length=1, max_length=4000)
    conversation_summary: str | None = Field(default=None, max_length=4000)
    dataset_profiles: list[DatasetProfileRef] = Field(default_factory=list, max_length=6)
    current_job: JobRef | None = None
    recent_jobs: list[JobRef] = Field(default_factory=list, max_length=20)
    decision: AgentDecision | None = None
    pending_interrupt: PendingInterrupt | None = None
    step_budget: StepBudget = Field(default_factory=StepBudget)

    @model_validator(mode="after")
    def _references_belong_to_user(self) -> "GraphState":
        references = [*self.dataset_profiles, *self.recent_jobs]
        if self.current_job is not None:
            references.append(self.current_job)
        if any(reference.owner_id != self.user_id for reference in references):
            raise ValueError("graph references must belong to user_id")
        return self
