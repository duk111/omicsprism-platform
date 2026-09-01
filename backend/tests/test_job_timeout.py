from datetime import datetime, timedelta, timezone

from backend.app.job_execution import RedisWorker
from backend.app.job_store import JobStorageService, LocalJsonJobRepository
from backend.app.models import AnalysisType, JobRecord, JobStatus


def _job(*, now: datetime, status: JobStatus = JobStatus.RUNNING) -> JobRecord:
    return JobRecord(
        id="job-timeout",
        project_name="timeout-test",
        analysis_type=AnalysisType.DEG,
        status=status,
        created_at=now - timedelta(minutes=10),
        updated_at=now - timedelta(minutes=10),
        started_at=now - timedelta(minutes=5) if status is JobStatus.RUNNING else None,
        progress=55,
        progress_step="Running analysis",
    )


def test_timeout_watchdog_marks_overdue_running_job_with_explicit_error(tmp_path) -> None:
    now = datetime.now(timezone.utc)
    store = JobStorageService(LocalJsonJobRepository(tmp_path))
    store.save(_job(now=now))
    worker = RedisWorker(
        object(),
        object(),
        job_store=store,
        job_timeout_seconds=60,
    )

    assert worker.expire_overdue_once(now=now) == 1
    expired = store.get_internal("job-timeout")
    assert expired.status is JobStatus.FAILED
    assert expired.error == "job_timeout"
    assert expired.progress == 55
    assert expired.progress_step == "Timed out"
    assert expired.completed_at == now
    assert worker.expire_overdue_once(now=now + timedelta(seconds=1)) == 0


def test_timeout_watchdog_does_not_touch_fresh_or_terminal_jobs(tmp_path) -> None:
    now = datetime.now(timezone.utc)
    store = JobStorageService(LocalJsonJobRepository(tmp_path))
    fresh = _job(now=now, status=JobStatus.QUEUED)
    fresh.created_at = now
    fresh.updated_at = now
    store.save(fresh)
    terminal = _job(now=now, status=JobStatus.FAILED)
    terminal.id = "job-terminal"
    terminal.error = "analysis_failed"
    store.save(terminal)
    worker = RedisWorker(object(), object(), job_store=store, job_timeout_seconds=60)

    assert worker.expire_overdue_once(now=now) == 0
    assert store.get_internal("job-timeout").status is JobStatus.QUEUED
    assert store.get_internal("job-terminal").error == "analysis_failed"
