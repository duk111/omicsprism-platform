from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from backend.app import main
from backend.app.job_store import JobStorageService, LocalJsonJobRepository
from backend.app.models import AnalysisType, JobRecord, JobStatus


def test_other_session_gets_404_for_another_users_job(tmp_path, monkeypatch) -> None:
    storage = JobStorageService(LocalJsonJobRepository(tmp_path))
    monkeypatch.setattr(main, "JOB_STORE", storage)

    with TestClient(main.app) as owner, TestClient(main.app) as other_user:
        owner.get("/api/jobs")
        owner_id = owner.cookies.get(main.SESSION_COOKIE)
        assert owner_id is not None

        now = datetime.now(timezone.utc)
        storage.save(
            JobRecord(
                id="private-job",
                project_name="private",
                analysis_type=AnalysisType.DIFFERENTIAL,
                status=JobStatus.QUEUED,
                created_at=now,
                updated_at=now,
                owner_id=owner_id,
            )
        )

        assert owner.get("/api/jobs/private-job").status_code == 200
        assert other_user.get("/api/jobs/private-job").status_code == 404
