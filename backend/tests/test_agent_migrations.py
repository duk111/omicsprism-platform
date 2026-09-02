from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_product_table_migration_defines_required_tables_and_constraints() -> None:
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


def test_role_migration_preserves_append_only_and_no_delete_contracts() -> None:
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


def test_control_plane_cleanup_drops_only_legacy_storage() -> None:
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


def test_analysis_type_rename_migration_updates_columns_and_payloads() -> None:
    sql = (
        ROOT / "migrations" / "007_rename_analysis_types.sql"
    ).read_text(encoding="utf-8").lower()

    assert "analysis_type = 'deg'" in sql
    assert "analysis_type = 'gma'" in sql
    assert "analysis_type = 'differential'" in sql
    assert "analysis_type = 'correlation'" in sql
    assert sql.count("jsonb_set(payload, '{analysis_type}'") == 2
    assert "analysis_type = 'dem'" not in sql


def test_state_and_lease_cleanup_migration_drops_zombie_columns() -> None:
    sql = (
        ROOT / "migrations" / "008_remove_legacy_run_state_and_turn_leases.sql"
    ).read_text(encoding="utf-8").lower()

    for column in ("active_profile", "state", "step_no", "model_calls", "tool_calls", "status"):
        assert f"drop column if exists {column}" in sql
    for column in ("lease_owner", "lease_expires_at"):
        assert f"drop column if exists {column}" in sql
    assert "drop index if exists agent_runs_status_idx" in sql
    assert "drop index if exists agent_turns_claim_idx" in sql


def test_checkpointer_migration_drops_custom_run_state_table() -> None:
    sql = (ROOT / "migrations" / "009_drop_agent_run_state_table.sql").read_text(encoding="utf-8").lower()

    assert "drop table if exists agent_runs;" in sql
    assert "drop table if exists agent_events;" in sql
    assert "where con.contype = 'f'" in sql
    assert "con.confrelid = 'public.agent_runs'::regclass" in sql
    assert "agent_threads" not in sql.split("drop table if exists agent_runs;")[1]
    assert "drop table if exists agent_runs cascade" not in sql


def test_trace_id_migration_adds_owned_turn_and_message_columns() -> None:
    sql = (ROOT / "migrations" / "013_agent_trace_id_columns.sql").read_text(encoding="utf-8").lower()
    assert "alter table agent_turns" in sql
    assert "alter table agent_messages" in sql
    assert "add column if not exists trace_id" in sql
    assert "alter column trace_id set not null" in sql
    assert "agent_turns_trace_idx" in sql
    assert "agent_messages_trace_idx" in sql


def test_job_completion_migration_defines_wait_and_outbox_contract() -> None:
    sql = (ROOT / "migrations" / "014_agent_job_completion_outbox.sql").read_text(encoding="utf-8").lower()
    assert "create table agent_job_waits" in sql
    assert "create table agent_job_events" in sql
    assert "unique (job_id, thread_id, user_id)" in sql
    assert "unique (job_id, status)" in sql
    assert "status in ('waiting', 'resume_queued', 'completed', 'failed', 'cancelled', 'expired')" in sql
    assert "status in ('succeeded', 'failed', 'cancelled')" in sql
    assert "grant select, insert, update on agent_job_waits to omics_app" in sql
    assert "grant select, insert, update on agent_job_events to omics_app" in sql
    assert "foreign key (turn_id)" in sql
    assert "references agent_turns (turn_id) on delete cascade" in sql
    assert "references agent_turns (turn_id, user_id)" not in sql


def test_feedback_candidate_migration_is_owned_and_review_gated() -> None:
    sql = (ROOT / "migrations" / "015_agent_feedback_candidates.sql").read_text(encoding="utf-8").lower()
    assert "create table agent_feedback" in sql
    assert "create table agent_eval_candidates" in sql
    assert "unique (message_id, user_id)" in sql
    assert "references agent_threads (thread_id, user_id) on delete cascade" in sql
    assert "references agent_feedback (feedback_id) on delete cascade" in sql
    assert "pending_review" in sql
    assert "grant select, insert, update on agent_feedback to omics_app" in sql
    assert "grant select, insert, update on agent_eval_candidates to omics_app" in sql
