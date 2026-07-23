from __future__ import annotations

import os
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException

from backend.app.job_store import PostgresJobRepository
from backend.app.models import AnalysisType, JobRecord, JobStatus


ADMIN_DSN = os.getenv("OMICS_PRISM_TEST_DATABASE_URL")
APP_DSN = os.getenv("OMICS_PRISM_TEST_APP_DATABASE_URL")
APP_PASSWORD = os.getenv("OMICS_PRISM_APP_DB_PASSWORD")
HAS_TEST_DATABASE = bool(ADMIN_DSN and APP_DSN and APP_PASSWORD)


@pytest.mark.skipif(
    not HAS_TEST_DATABASE,
    reason="需要专用 PostgreSQL 测试库和 OMICS_PRISM_TEST_* 环境变量",
)
def test_runtime_role_can_use_jobs_but_cannot_change_schema() -> None:
    import psycopg
    from psycopg.errors import InsufficientPrivilege

    from scripts.migrate import apply_migrations

    assert ADMIN_DSN is not None
    assert APP_DSN is not None
    assert APP_PASSWORD is not None
    apply_migrations(ADMIN_DSN, APP_PASSWORD)

    job_id = f"job-{uuid4()}"
    user_id = f"user-{uuid4()}"
    now = datetime.now(timezone.utc)
    job = JobRecord(
        id=job_id,
        project_name="runtime-role-test",
        analysis_type=AnalysisType.DIFFERENTIAL,
        status=JobStatus.QUEUED,
        created_at=now,
        updated_at=now,
        owner_id=user_id,
    )

    try:
        repository = PostgresJobRepository(APP_DSN)
        repository.save(job)
        assert repository.get(job_id, user_id).id == job_id
        assert [item.id for item in repository.list_for_user(user_id)] == [job_id]
        job.status = JobStatus.RUNNING
        job.updated_at = datetime.now(timezone.utc)
        repository.save(job)
        assert repository.get(job_id, user_id).status is JobStatus.RUNNING
        with pytest.raises(HTTPException) as missing:
            repository.get(job_id, f"other-{uuid4()}")
        assert missing.value.status_code == 404

        with psycopg.connect(APP_DSN, autocommit=True) as conn:
            with pytest.raises(InsufficientPrivilege):
                conn.execute("alter table jobs add column runtime_role_test text")
            with pytest.raises(InsufficientPrivilege):
                conn.execute("delete from jobs where id = %s", (job_id,))
    finally:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
            conn.execute("delete from jobs where id = %s", (job_id,))
