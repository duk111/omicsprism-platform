from __future__ import annotations

import re
from collections.abc import Iterable
from uuid import uuid4

from .schemas import (
    AgentEvalCandidateExport,
    AgentEvalCandidateRecord,
    AgentEvalCandidateStatus,
    AgentEvalTraceSummary,
    AgentFeedbackRecord,
    AgentMessageRecord,
)
from .trace import AgentTraceEvent


_DSN_PATTERN = re.compile(r"(?i)\b(?:postgres(?:ql)?|redis|mysql|mongodb)://[^\s]+")
_OBJECT_KEY_PATTERN = re.compile(r"(?i)\b(?:s3://|agent-inputs/|runs/|artifacts/)[^\s,;]+")
_SECRET_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|token|password|secret)\s*([=:])\s*[^\s,;]+"
)
_EMAIL_PATTERN = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
_IP_PATTERN = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_WINDOWS_PATH_PATTERN = re.compile(r"(?i)\b[a-z]:\\[^\s,;]+")
_UNIX_PATH_PATTERN = re.compile(r"(?<!:)\B/(?:[^\s,;]+/)+[^\s,;]+")


def build_eval_candidate(
    *,
    feedback: AgentFeedbackRecord,
    user_message: AgentMessageRecord | None,
    assistant_message: AgentMessageRecord,
    trace_events: Iterable[AgentTraceEvent],
) -> AgentEvalCandidateRecord:
    """Create a redacted, non-golden candidate from owned negative feedback."""

    if not requires_eval_review(feedback):
        raise ValueError("helpful feedback without a correction is not an eval candidate")
    return AgentEvalCandidateRecord(
        candidate_id=f"candidate-{uuid4()}",
        feedback_id=feedback.feedback_id,
        thread_id=feedback.thread_id,
        turn_id=feedback.turn_id,
        message_id=feedback.message_id,
        trace_id=feedback.trace_id,
        user_id=feedback.user_id,
        status=AgentEvalCandidateStatus.PENDING_REVIEW,
        rating=feedback.rating,
        failure_category=feedback.failure_category,
        user_message_summary=_message_summary(user_message, fallback="No user message was retained for review."),
        assistant_message_summary=_message_summary(
            assistant_message,
            fallback="No assistant message was retained for review.",
        ),
        correction_summary=(
            redact_candidate_text(feedback.correction_text)
            if feedback.correction_text is not None
            else None
        ),
        trace_summary=summarize_trace(trace_events),
        created_at=feedback.created_at,
        updated_at=feedback.updated_at,
    )


def requires_eval_review(feedback: AgentFeedbackRecord) -> bool:
    """Only a reported failure or explicit correction can enter human review."""

    return (
        feedback.rating.value == "unhelpful"
        or feedback.correction_text is not None
    )


def export_eval_candidate(candidate: AgentEvalCandidateRecord) -> AgentEvalCandidateExport:
    """Drop every tenant and source identifier before human-review export."""

    return AgentEvalCandidateExport(
        candidate_id=candidate.candidate_id,
        rating=candidate.rating,
        failure_category=candidate.failure_category,
        user_message_summary=candidate.user_message_summary,
        assistant_message_summary=candidate.assistant_message_summary,
        correction_summary=candidate.correction_summary,
        trace_summary=candidate.trace_summary,
        created_at=candidate.created_at,
    )


def redact_candidate_text(text: str) -> str:
    """Keep concise language context while excluding secrets, paths, and tables."""

    normalized = text.strip()
    lines = [line for line in normalized.splitlines() if line.strip()]
    if len(lines) >= 3 and sum("," in line for line in lines[:6]) >= 3:
        return "[tabular content omitted]"
    value = _DSN_PATTERN.sub("[connection string omitted]", normalized)
    value = _OBJECT_KEY_PATTERN.sub("[storage reference omitted]", value)
    value = _SECRET_PATTERN.sub(r"\1\2[redacted]", value)
    value = _EMAIL_PATTERN.sub("[email omitted]", value)
    value = _IP_PATTERN.sub("[ip omitted]", value)
    value = _WINDOWS_PATH_PATTERN.sub("[path omitted]", value)
    value = _UNIX_PATH_PATTERN.sub("[path omitted]", value)
    value = " ".join(value.split())
    return value[:1200] or "[empty feedback text]"


def summarize_trace(events: Iterable[AgentTraceEvent]) -> AgentEvalTraceSummary:
    event_list = list(events)
    return AgentEvalTraceSummary(
        event_types=sorted({event.event_type for event in event_list}),
        model_calls=sum(event.event_type == "model.call" for event in event_list),
        tool_calls=sum(event.event_type == "tool.call" for event in event_list),
        total_latency_ms=round(
            sum(event.latency_ms or 0 for event in event_list),
            3,
        ),
        error_codes=sorted({
            event.error_code for event in event_list if event.error_code
        }),
    )


def _message_summary(
    message: AgentMessageRecord | None,
    *,
    fallback: str,
) -> str:
    if message is None:
        return fallback
    text = " ".join(
        block.text
        for block in message.blocks
        if getattr(block, "type", None) in {"text", "advisory"}
    )
    return redact_candidate_text(text) if text else fallback
