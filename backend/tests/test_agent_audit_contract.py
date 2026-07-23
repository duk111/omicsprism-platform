from __future__ import annotations

import inspect
from pathlib import Path

from backend.app.agent.audit import AgentEventStore


ROOT = Path(__file__).resolve().parents[2]


def test_application_event_store_exposes_only_append_and_select() -> None:
    public_methods = {
        name
        for name, value in inspect.getmembers(AgentEventStore, inspect.isfunction)
        if not name.startswith("_")
    }

    assert public_methods == {"append", "list_for_run"}


def test_role_migration_declares_append_only_agent_events_permissions() -> None:
    sql = (ROOT / "migrations" / "002_agent_roles.sql").read_text(encoding="utf-8").upper()

    assert "GRANT SELECT, INSERT ON AGENT_EVENTS TO OMICS_APP" in sql
    assert "REVOKE UPDATE, DELETE ON AGENT_EVENTS FROM OMICS_APP" in sql
    assert "GRANT SELECT, INSERT, UPDATE ON AGENT_RUNS TO OMICS_APP" in sql
    assert "GRANT SELECT, INSERT, UPDATE, DELETE ON AGENT_EVENTS" not in sql
