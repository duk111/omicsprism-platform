from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Literal, Protocol

from opentelemetry import metrics, trace as otel_trace
from pydantic import Field

from .schemas import ContractModel


LOG = logging.getLogger("omicsprism.platform.agent_trace")

GRAPH_VERSION = "agent-graph.v3"
PROMPT_VERSION = "main-system.v1"
MODEL_PROVIDER = "vllm-openai-compatible"
TOOL_SCHEMA_VERSION = "readonly-tools.v1"


class ModelUsage(ContractModel):
    """Provider-reported usage; absent provider fields remain unknown (null)."""

    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cached_tokens: int | None = Field(default=None, ge=0)
    status: Literal["reported", "unknown"] = "unknown"


class AgentTraceEvent(ContractModel):
    """Minimal vendor-neutral trace event with no raw prompt, rows, or secrets."""

    event_id: str = Field(min_length=1, max_length=200)
    trace_id: str = Field(min_length=1, max_length=200)
    thread_id: str = Field(min_length=1, max_length=200)
    turn_id: str | None = Field(default=None, max_length=200)
    run_id: str | None = Field(default=None, max_length=200)
    user_id: str = Field(min_length=1, max_length=200)
    event_type: Literal[
        "turn.queued",
        "turn.started",
        "turn.completed",
        "turn.failed",
        "model.call",
        "tool.call",
        "job.submitted",
    ]
    component: Literal["api", "runtime", "graph", "model", "tool", "job"]
    name: str = Field(min_length=1, max_length=200)
    schema_version: str = Field(min_length=1, max_length=80)
    graph_version: str = Field(default=GRAPH_VERSION, min_length=1, max_length=80)
    prompt_version: str | None = Field(default=None, max_length=80)
    prompt_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-fA-F]{64}$")
    model_provider: str | None = Field(default=None, max_length=100)
    model_name: str | None = Field(default=None, max_length=200)
    tool_name: str | None = Field(default=None, max_length=200)
    tool_schema_hash: str | None = Field(default=None, pattern=r"^sha256:[0-9a-fA-F]{64}$")
    job_id: str | None = Field(default=None, max_length=200)
    outcome: str | None = Field(default=None, max_length=100)
    latency_ms: float | None = Field(default=None, ge=0)
    prompt_tokens: int | None = Field(default=None, ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cached_tokens: int | None = Field(default=None, ge=0)
    usage_status: Literal["reported", "unknown"] | None = None
    retry_count: int = Field(default=0, ge=0)
    error_code: str | None = Field(default=None, max_length=100)
    created_at: datetime


class TraceObserver(Protocol):
    """Vendor-neutral telemetry boundary for safe trace summaries."""

    def record(self, event: AgentTraceEvent) -> None:
        ...


class OpenTelemetryTraceObserver:
    """Emit safe event spans and metrics through the OpenTelemetry API."""

    def __init__(self) -> None:
        self._tracer = otel_trace.get_tracer("omicsprism.agent")
        meter = metrics.get_meter("omicsprism.agent")
        self._events = meter.create_counter("omicsprism.agent.trace_events", unit="1")
        self._latency = meter.create_histogram("omicsprism.agent.operation_latency", unit="ms")

    def record(self, event: AgentTraceEvent) -> None:
        attributes = _telemetry_attributes(event)
        with self._tracer.start_as_current_span(
            f"omicsprism.agent.{event.event_type}",
            attributes=attributes,
        ):
            self._events.add(1, attributes)
            if event.latency_ms is not None:
                self._latency.record(event.latency_ms, attributes)


def stable_hash(value: object) -> str:
    canonical = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def schema_hash(model: Any) -> str:
    return stable_hash(model.model_json_schema())


def prompt_hash(system_prompt: str, context_payload: object) -> str:
    return stable_hash({"system": system_prompt, "context": context_payload})


class TraceRecorder:
    """Best-effort persistence adapter shared by model, tool, and runtime paths."""

    def __init__(
        self,
        sink: Callable[[AgentTraceEvent], None],
        observer: TraceObserver | None = None,
    ) -> None:
        self.sink = sink
        self.observer = observer or OpenTelemetryTraceObserver()

    def record(self, event: AgentTraceEvent) -> None:
        try:
            self.sink(event)
        except Exception:
            LOG.exception(
                "failed to persist agent trace event",
                extra={"event": "agent.trace.persist_failed", "trace_id": event.trace_id},
            )
        try:
            self.observer.record(event)
        except Exception:
            LOG.exception(
                "failed to emit agent telemetry",
                extra={"event": "agent.trace.telemetry_failed", "trace_id": event.trace_id},
            )

    def turn_event(
        self,
        *,
        event_type: Literal["turn.queued", "turn.started", "turn.completed", "turn.failed"],
        trace_id: str,
        thread_id: str,
        user_id: str,
        turn_id: str,
        run_id: str | None = None,
        outcome: str | None = None,
        latency_ms: float | None = None,
        retry_count: int = 0,
        error_code: str | None = None,
    ) -> None:
        self.record(AgentTraceEvent(
            event_id=_event_id(),
            trace_id=trace_id,
            thread_id=thread_id,
            turn_id=turn_id,
            run_id=run_id,
            user_id=user_id,
            event_type=event_type,
            component="api" if event_type == "turn.queued" else "runtime",
            name=event_type,
            schema_version="agent-turn.v1",
            outcome=outcome,
            latency_ms=latency_ms,
            retry_count=retry_count,
            error_code=error_code,
            created_at=datetime.now(timezone.utc),
        ))

    def tool_call(
        self,
        *,
        context: Any,
        tool_name: str,
        tool_schema_hash: str,
        latency_ms: float,
        outcome: str,
        error_code: str | None = None,
    ) -> None:
        self.record(AgentTraceEvent(
            event_id=_event_id(),
            trace_id=context.trace_id,
            thread_id=context.thread_id,
            turn_id=context.turn_id,
            run_id=context.run_id,
            user_id=context.user_id,
            event_type="tool.call",
            component="tool",
            name=tool_name,
            schema_version=TOOL_SCHEMA_VERSION,
            tool_name=tool_name,
            tool_schema_hash=tool_schema_hash,
            outcome=outcome,
            latency_ms=latency_ms,
            error_code=error_code,
            created_at=datetime.now(timezone.utc),
        ))

    def job_submitted(
        self,
        *,
        request: Any,
        job_id: str,
        latency_ms: float,
        outcome: str,
        error_code: str | None = None,
    ) -> None:
        self.record(AgentTraceEvent(
            event_id=_event_id(),
            trace_id=request.trace_id,
            thread_id=request.thread_id,
            turn_id=request.turn_id,
            run_id=request.run_id,
            user_id=request.user_id,
            event_type="job.submitted",
            component="job",
            name="job.submit",
            schema_version="analysis-job.v1",
            job_id=job_id,
            outcome=outcome,
            latency_ms=latency_ms,
            error_code=error_code,
            created_at=datetime.now(timezone.utc),
        ))

    def model_call(
        self,
        *,
        context: Any,
        model_name: str,
        system_prompt: str,
        schema_version: str,
        usage: ModelUsage,
        latency_ms: float,
        retry_count: int,
        outcome: str,
        error_code: str | None = None,
    ) -> None:
        self.record(AgentTraceEvent(
            event_id=_event_id(),
            trace_id=context.trace_id,
            thread_id=context.thread_id,
            turn_id=context.turn_id,
            run_id=context.run_id,
            user_id=context.user_id,
            event_type="model.call",
            component="model",
            name="chat.completions",
            schema_version=schema_version,
            prompt_version=PROMPT_VERSION,
            prompt_hash=prompt_hash(system_prompt, context.model_dump(mode="json")),
            model_provider=MODEL_PROVIDER,
            model_name=model_name,
            outcome=outcome,
            latency_ms=latency_ms,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
            cached_tokens=usage.cached_tokens,
            usage_status=usage.status,
            retry_count=retry_count,
            error_code=error_code,
            created_at=datetime.now(timezone.utc),
        ))


def _event_id() -> str:
    from uuid import uuid4

    return f"trace-event-{uuid4()}"


def _telemetry_attributes(event: AgentTraceEvent) -> dict[str, str | int | float]:
    attributes: dict[str, str | int | float] = {
        "omicsprism.trace_id": event.trace_id,
        "omicsprism.thread_id": event.thread_id,
        "omicsprism.event_type": event.event_type,
        "omicsprism.component": event.component,
        "omicsprism.name": event.name,
        "omicsprism.graph_version": event.graph_version,
        "omicsprism.schema_version": event.schema_version,
        "omicsprism.retry_count": event.retry_count,
    }
    for key, value in {
        "omicsprism.turn_id": event.turn_id,
        "omicsprism.run_id": event.run_id,
        "omicsprism.prompt_version": event.prompt_version,
        "omicsprism.model_provider": event.model_provider,
        "omicsprism.model_name": event.model_name,
        "omicsprism.tool_name": event.tool_name,
        "omicsprism.job_id": event.job_id,
        "omicsprism.outcome": event.outcome,
        "omicsprism.error_code": event.error_code,
        "gen_ai.usage.input_tokens": event.prompt_tokens,
        "gen_ai.usage.output_tokens": event.completion_tokens,
        "gen_ai.usage.total_tokens": event.total_tokens,
    }.items():
        if value is not None:
            attributes[key] = value
    return attributes
