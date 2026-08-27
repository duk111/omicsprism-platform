from __future__ import annotations

import json
import logging

from backend.app.observability import JsonLogFormatter


def test_agent_log_fields_are_serialized_without_audit_lifecycle() -> None:
    record = logging.LogRecord(
        name="omicsprism.platform",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="agent graph action",
        args=(),
        exc_info=None,
    )
    record.user_id = "user-1"
    record.job_id = "job-1"
    record.thread_id = "thread-1"
    record.action = "resume"
    record.node = "confirmation"
    record.artifact = "results.csv"
    record.error_code = "graph_execution_failed"
    record.duration_ms = 12.5

    payload = json.loads(JsonLogFormatter().format(record))

    assert payload | {"ts": None} == {
        "ts": None,
        "level": "INFO",
        "logger": "omicsprism.platform",
        "message": "agent graph action",
        "request_id": None,
        "job_id": "job-1",
        "user_id": "user-1",
        "project_id": None,
        "duration_ms": 12.5,
        "action": "resume",
        "thread_id": "thread-1",
        "node": "confirmation",
        "artifact": "results.csv",
        "error_code": "graph_execution_failed",
    }
