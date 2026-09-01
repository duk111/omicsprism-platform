from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from backend.app.agent.job_events import (
    AgentJobCompletionEvent,
    AgentJobWaitRecord,
    completion_event_id,
)
from backend.app.models import JobStatus


def _wait() -> AgentJobWaitRecord:
    now = datetime.now(timezone.utc)
    return AgentJobWaitRecord(
        wait_id="wait-1",
        thread_id="thread-1",
        user_id="user-1",
        turn_id="turn-1",
        run_id="run-1",
        trace_id="trace-1",
        job_id="job-1",
        created_at=now,
        updated_at=now,
    )


def test_job_wait_is_bound_to_all_agent_ownership_fields() -> None:
    wait = _wait()
    assert wait.user_id == "user-1"
    assert wait.thread_id == "thread-1"
    assert wait.turn_id == "turn-1"
    assert wait.run_id == "run-1"
    assert wait.trace_id == "trace-1"
    assert wait.status == "waiting"


def test_completion_event_has_stable_id_and_terminal_status() -> None:
    event = AgentJobCompletionEvent(
        event_id=completion_event_id("job-1", JobStatus.SUCCEEDED),
        job_id="job-1",
        thread_id="thread-1",
        user_id="user-1",
        turn_id="turn-1",
        run_id="run-1",
        trace_id="trace-1",
        status=JobStatus.SUCCEEDED,
        occurred_at=datetime.now(timezone.utc),
    )
    assert event.event_type == "job.completed"
    assert event.error_code is None


def test_completion_event_rejects_running_and_unstable_ids() -> None:
    with pytest.raises(ValueError, match="terminal"):
        AgentJobCompletionEvent(
            event_id="job-completion:job-1:running",
            job_id="job-1",
            thread_id="thread-1",
            user_id="user-1",
            turn_id="turn-1",
            run_id="run-1",
            trace_id="trace-1",
            status=JobStatus.RUNNING,
            occurred_at=datetime.now(timezone.utc),
        )
    with pytest.raises(ValidationError, match="stable"):
        AgentJobCompletionEvent(
            event_id="event-random",
            job_id="job-1",
            thread_id="thread-1",
            user_id="user-1",
            turn_id="turn-1",
            run_id="run-1",
            trace_id="trace-1",
            status=JobStatus.FAILED,
            occurred_at=datetime.now(timezone.utc),
        )


def test_completion_event_id_rejects_non_terminal_status() -> None:
    with pytest.raises(ValueError, match="terminal"):
        completion_event_id("job-1", JobStatus.RUNNING)
