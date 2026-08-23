from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_phase6_product_table_migration_defines_required_tables_and_constraints() -> None:
    sql = (ROOT / "migrations" / "004_agent_product_tables.sql").read_text(encoding="utf-8").lower()

    for table in (
        "agent_threads",
        "agent_messages",
        "agent_turns",
        "agent_plans",
        "agent_approvals",
        "agent_input_bundles",
        "agent_input_files",
    ):
        assert f"create table {table}" in sql
    assert "agent_turns_user_idempotency_idx" in sql
    assert "agent_turns_one_active_per_thread_idx" in sql
    assert "where status in ('queued', 'running')" in sql


def test_phase6_role_migration_preserves_append_only_and_no_delete_contracts() -> None:
    sql = (ROOT / "migrations" / "005_agent_product_roles.sql").read_text(encoding="utf-8").lower()

    assert "grant select, insert on agent_messages to omics_app" in sql
    assert "revoke update, delete" in sql and "agent_messages" in sql
    assert "grant select, insert on agent_events to omics_app" in sql
    for table in (
        "agent_threads",
        "agent_turns",
        "agent_plans",
        "agent_approvals",
        "agent_input_bundles",
        "agent_input_files",
    ):
        assert table in sql
    assert "revoke delete" in sql


def test_phase5_cleanup_drops_only_legacy_control_plane_storage() -> None:
    sql = (
        ROOT / "migrations" / "006_drop_legacy_agent_control_plane.sql"
    ).read_text(encoding="utf-8").lower()

    assert "drop table agent_approvals" in sql
    assert "drop table agent_plans" in sql
    for column in ("plan_id", "plan_hash", "pending_approval_id"):
        assert f"drop column {column}" in sql
    for retained in (
        "agent_runs",
        "agent_threads",
        "agent_messages",
        "agent_turns",
        "agent_input_bundles",
        "agent_input_files",
        "jobs",
        "artifacts",
        "agent_events",
    ):
        assert f"drop table {retained}" not in sql


def test_phase5_analysis_type_migration_updates_columns_and_payloads() -> None:
    sql = (
        ROOT / "migrations" / "007_rename_analysis_types.sql"
    ).read_text(encoding="utf-8").lower()

    assert "analysis_type = 'deg'" in sql
    assert "analysis_type = 'gma'" in sql
    assert "analysis_type = 'differential'" in sql
    assert "analysis_type = 'correlation'" in sql
    assert sql.count("jsonb_set(payload, '{analysis_type}'") == 2
    assert "analysis_type = 'dem'" not in sql
