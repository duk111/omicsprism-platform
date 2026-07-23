from __future__ import annotations

from pathlib import Path

import pytest

from backend.app.bootstrap import create_context
from backend.app.settings import AppSettings


ROOT = Path(__file__).resolve().parents[2]


def test_postgres_storage_requires_dedicated_runtime_dsn(tmp_path) -> None:
    settings = AppSettings(
        storage_backend="postgres",
        runs_dir=tmp_path / "runs",
        file_storage_root=tmp_path / "storage",
    )

    with pytest.raises(RuntimeError, match="OMICS_PRISM_RUNTIME_DATABASE_URL"):
        create_context(
            settings,
            ensure_figure_specs=lambda _job_id: {},
            remaining_seconds=lambda _job, _progress: None,
            include_executor=False,
        )


def test_jobs_migration_grants_runtime_access_without_schema_mutation() -> None:
    sql = (ROOT / "migrations" / "003_runtime_jobs.sql").read_text(encoding="utf-8").upper()

    assert "CREATE TABLE IF NOT EXISTS JOBS" in sql
    assert "GRANT SELECT, INSERT, UPDATE ON JOBS TO OMICS_APP" in sql
    assert "REVOKE DELETE, TRUNCATE, REFERENCES, TRIGGER ON JOBS FROM OMICS_APP" in sql
    assert "REVOKE CREATE ON SCHEMA PUBLIC FROM OMICS_APP" in sql
