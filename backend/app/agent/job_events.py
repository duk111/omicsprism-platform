from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import Field, model_validator

from ..models import JobStatus
from .schemas import ContractModel


class AgentJobWaitStatus(str, Enum):
    """Durable lifecycle for a graph waiting on an analysis Job."""

    WAITING = "waiting"
    RESUME_QUEUED = "resume_queued"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


TerminalJobStatus = Literal[
    JobStatus.SUCCEEDED,
    JobStatus.FAILED,
    JobStatus.CANCELLED,
]


class AgentJobWaitRecord(ContractModel):
    """Ownership-bound durable subscription from an Agent turn to a Job."""

    wait_id: str = Field(min_length=1, max_length=200)
    thread_id: str = Field(min_length=1, max_length=200)
    user_id: str = Field(min_length=1, max_length=200)
    turn_id: str = Field(min_length=1, max_length=200)
    run_id: str = Field(min_length=1, max_length=200)
    trace_id: str = Field(min_length=1, max_length=200)
    job_id: str = Field(min_length=1, max_length=200)
    status: AgentJobWaitStatus = AgentJobWaitStatus.WAITING
    continuation_turn_id: str | None = Field(default=None, max_length=200)
    expires_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class AgentJobCompletionEvent(ContractModel):
    """Durable, at-least-once event emitted for a terminal Job state."""

    event_id: str = Field(min_length=1, max_length=300)
    event_type: Literal["job.completed"] = "job.completed"
    job_id: str = Field(min_length=1, max_length=200)
    thread_id: str = Field(min_length=1, max_length=200)
    user_id: str = Field(min_length=1, max_length=200)
    turn_id: str = Field(min_length=1, max_length=200)
    run_id: str = Field(min_length=1, max_length=200)
    trace_id: str = Field(min_length=1, max_length=200)
    status: JobStatus
    error_code: str | None = Field(default=None, max_length=200)
    attempt: int = Field(default=0, ge=0)
    occurred_at: datetime

    @model_validator(mode="after")
    def _terminal_status_only(self) -> "AgentJobCompletionEvent":
        if self.status not in {
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }:
            raise ValueError("job completion events require a terminal Job status")
        expected_id = f"job-completion:{self.job_id}:{self.status.value}"
        if self.event_id != expected_id:
            raise ValueError("event_id must be stable for the Job terminal state")
        return self


def completion_event_id(job_id: str, status: JobStatus) -> str:
    """Return the idempotency key shared by all deliveries of one terminal state."""

    if status not in {
        JobStatus.SUCCEEDED,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
    }:
        raise ValueError("completion event requires a terminal Job status")
    return f"job-completion:{job_id}:{status.value}"
