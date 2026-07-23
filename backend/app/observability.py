from __future__ import annotations

import json
import logging
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator, Protocol
from uuid import uuid4

from .models import AuditEventRecord, JobRecord, JobStatus


_request_id: ContextVar[str | None] = ContextVar("request_id", default=None)
_job_id: ContextVar[str | None] = ContextVar("job_id", default=None)
_user_id: ContextVar[str | None] = ContextVar("user_id", default=None)
_project_id: ContextVar[str | None] = ContextVar("project_id", default=None)


LOG = logging.getLogger("omicsprism.platform")


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "request_id": getattr(record, "request_id", None) or _request_id.get(),
            "job_id": getattr(record, "job_id", None) or _job_id.get(),
            "user_id": getattr(record, "user_id", None) or _user_id.get(),
            "project_id": getattr(record, "project_id", None) or _project_id.get(),
        }
        for key in ("method", "path", "status_code", "duration_ms", "event", "action"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = getattr(record, "request_id", None) or _request_id.get()
        record.job_id = getattr(record, "job_id", None) or _job_id.get()
        record.user_id = getattr(record, "user_id", None) or _user_id.get()
        record.project_id = getattr(record, "project_id", None) or _project_id.get()
        return True


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonLogFormatter())
    handler.addFilter(ContextFilter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    for noisy in ("uvicorn.access",):
        logging.getLogger(noisy).handlers = []
        logging.getLogger(noisy).propagate = True


def current_request_id() -> str | None:
    return _request_id.get()


@contextmanager
def log_context(
    *,
    request_id: str | None = None,
    job_id: str | None = None,
    user_id: str | None = None,
    project_id: str | None = None,
) -> Iterator[None]:
    tokens = []
    if request_id is not None:
        tokens.append((_request_id, _request_id.set(request_id)))
    if job_id is not None:
        tokens.append((_job_id, _job_id.set(job_id)))
    if user_id is not None:
        tokens.append((_user_id, _user_id.set(user_id)))
    if project_id is not None:
        tokens.append((_project_id, _project_id.set(project_id)))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def bind_request_context(request_id: str, *, user_id: str | None = None, project_id: str | None = None) -> None:
    _request_id.set(request_id)
    if user_id is not None:
        _user_id.set(user_id)
    if project_id is not None:
        _project_id.set(project_id)


class AuditRepository(Protocol):
    def record(self, event: AuditEventRecord) -> None:
        ...

    def list(self, limit: int = 100, user_id: str | None = None, job_id: str | None = None) -> list[AuditEventRecord]:
        ...


@dataclass
class LocalAuditRepository:
    events: list[AuditEventRecord] = field(default_factory=list)

    def record(self, event: AuditEventRecord) -> None:
        self.events.append(event)

    def list(self, limit: int = 100, user_id: str | None = None, job_id: str | None = None) -> list[AuditEventRecord]:
        rows = self.events
        if user_id is not None:
            rows = [item for item in rows if item.user_id == user_id]
        if job_id is not None:
            rows = [item for item in rows if item.job_id == job_id]
        return sorted(rows, key=lambda item: item.created_at, reverse=True)[:limit]


class AuditService:
    def __init__(self, repository: AuditRepository) -> None:
        self.repository = repository

    def record(
        self,
        action: str,
        *,
        event_type: str = "user_action",
        job: JobRecord | None = None,
        job_id: str | None = None,
        user_id: str | None = None,
        project_id: str | None = None,
        request_id: str | None = None,
        status_from: JobStatus | str | None = None,
        status_to: JobStatus | str | None = None,
        entity_type: str | None = None,
        entity_id: str | None = None,
        message: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if job is not None:
            job_id = job_id or job.id
            user_id = user_id or job.owner_id or None
            project_id = project_id or job.project_id or job.id
        event = AuditEventRecord(
            id=str(uuid4()),
            event_type=event_type,
            action=action,
            job_id=job_id,
            user_id=user_id or _user_id.get(),
            project_id=project_id or _project_id.get(),
            request_id=request_id or _request_id.get(),
            status_from=_status_value(status_from),
            status_to=_status_value(status_to),
            entity_type=entity_type,
            entity_id=entity_id,
            message=message,
            metadata=metadata or {},
            created_at=datetime.now(timezone.utc),
        )
        try:
            self.repository.record(event)
        except Exception:
            LOG.exception("failed to record audit event", extra={"event": event.event_type, "action": event.action, "job_id": event.job_id})
            return
        LOG.info("audit event", extra={"event": event.event_type, "action": event.action, "job_id": event.job_id})

    def list(self, limit: int = 100, user_id: str | None = None, job_id: str | None = None) -> list[AuditEventRecord]:
        return self.repository.list(limit=limit, user_id=user_id, job_id=job_id)


def audit_job_status(audit: AuditService | None, *, job: JobRecord, previous_status: JobStatus | None) -> None:
    if audit is None or previous_status == job.status:
        return
    audit.record(
        f"job.{job.status.value}",
        event_type="job_status",
        job=job,
        status_from=previous_status,
        status_to=job.status,
        entity_type="job",
        entity_id=job.id,
        metadata={"progress": job.progress, "attempt": job.attempt},
    )


def _status_value(value: JobStatus | str | None) -> str | None:
    if isinstance(value, JobStatus):
        return value.value
    return value
