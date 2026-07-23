from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.app.job_store import JobStorageService, LocalJsonJobRepository
from backend.app.models import AnalysisType, JobRecord, JobStatus


def _job(job_id: str, user_id: str) -> JobRecord:
    now = datetime.now(timezone.utc)
    return JobRecord(
        id=job_id,
        project_name="test",
        analysis_type=AnalysisType.DIFFERENTIAL,
        status=JobStatus.QUEUED,
        created_at=now,
        updated_at=now,
        owner_id=user_id,
    )


def test_get_requires_user_id_and_returns_404_for_cross_user(tmp_path) -> None:
    repository = LocalJsonJobRepository(tmp_path)
    repository.save(_job("job-a", "user-a"))

    with pytest.raises(Exception) as exc_info:
        repository.get("job-a", "user-b")

    assert getattr(exc_info.value, "status_code", None) == 404
    assert repository.get("job-a", "user-a").owner_id == "user-a"


def test_list_for_user_does_not_return_other_users(tmp_path) -> None:
    repository = LocalJsonJobRepository(tmp_path)
    repository.save(_job("job-a", "user-a"))
    repository.save(_job("job-b", "user-b"))

    assert [job.id for job in repository.list_for_user("user-a")] == ["job-a"]


def test_storage_service_user_bound_read_is_the_agent_safe_entrypoint(tmp_path) -> None:
    storage = JobStorageService(LocalJsonJobRepository(tmp_path))
    storage.save(_job("job-a", "user-a"))

    assert storage.get_for_user("job-a", "user-a").owner_id == "user-a"
    with pytest.raises(Exception) as exc_info:
        storage.get_for_user("job-a", "user-b")
    assert getattr(exc_info.value, "status_code", None) == 404
