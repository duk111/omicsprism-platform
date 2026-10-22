from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.app.job_execution import RedisWorker, publish_job
from backend.app.job_store import JobStorageService, LocalJsonJobRepository
from backend.app.models import AnalysisType, JobRecord, JobStatus


def _job(job_id: str = "job-dispatch", *, status: JobStatus = JobStatus.QUEUED) -> JobRecord:
    now = datetime.now(timezone.utc)
    return JobRecord(
        id=job_id,
        project_name="dispatch-test",
        analysis_type=AnalysisType.DEG,
        status=status,
        created_at=now,
        updated_at=now,
        dispatch_state="pending",
    )


class _Executor:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.enqueued: list[str] = []

    def enqueue(self, job_id: str) -> None:
        if self.fail:
            raise RuntimeError("redis unavailable")
        self.enqueued.append(job_id)


class _Queue:
    def __init__(self, *, failures: int = 0) -> None:
        self.failures = failures
        self.enqueued: list[str] = []

    def enqueue(self, job_id: str) -> None:
        if self.failures:
            self.failures -= 1
            raise RuntimeError("redis unavailable")
        self.enqueued.append(job_id)


class _FailOnceRepository(LocalJsonJobRepository):
    def __init__(self, root) -> None:
        super().__init__(root)
        self.fail_next_save = False

    def save(self, job: JobRecord) -> None:
        if self.fail_next_save:
            self.fail_next_save = False
            raise OSError("database unavailable")
        super().save(job)


def test_publish_failure_keeps_durable_job_pending(tmp_path) -> None:
    store = JobStorageService(LocalJsonJobRepository(tmp_path))
    job = _job()
    store.save(job)

    with pytest.raises(RuntimeError, match="redis unavailable"):
        publish_job(store, _Executor(fail=True), job)

    persisted = store.get_internal(job.id)
    assert persisted.status is JobStatus.QUEUED
    assert persisted.dispatch_state == "pending"


def test_compensation_retries_pending_job_after_redis_recovers(tmp_path) -> None:
    store = JobStorageService(LocalJsonJobRepository(tmp_path))
    store.save(_job())
    queue = _Queue(failures=1)
    worker = RedisWorker(queue, object(), job_store=store, dispatch_retry_delay_seconds=0)

    assert worker.publish_pending_once() == 0
    failed = store.get_internal("job-dispatch")
    assert failed.dispatch_state == "pending"
    assert failed.dispatch_attempts == 1
    assert failed.dispatch_error == "redis unavailable"

    assert worker.publish_pending_once() == 1
    recovered = store.get_internal("job-dispatch")
    assert recovered.dispatch_state == "published"
    assert recovered.dispatch_error is None
    assert queue.enqueued == ["job-dispatch"]


def test_successful_enqueue_with_state_write_failure_remains_recoverable(tmp_path) -> None:
    repository = _FailOnceRepository(tmp_path)
    store = JobStorageService(repository)
    job = _job()
    store.save(job)
    repository.fail_next_save = True
    executor = _Executor()

    with pytest.raises(OSError, match="database unavailable"):
        publish_job(store, executor, job)

    assert executor.enqueued == [job.id]
    assert store.get_internal(job.id).dispatch_state == "pending"


def test_publish_state_write_preserves_worker_progress(tmp_path) -> None:
    store = JobStorageService(LocalJsonJobRepository(tmp_path))
    job = _job()
    store.save(job)

    class _ImmediateExecutor(_Executor):
        def enqueue(self, job_id: str) -> None:
            super().enqueue(job_id)
            assert store.claim_for_execution(job_id) is not None

    publish_job(store, _ImmediateExecutor(), job)

    persisted = store.get_internal(job.id)
    assert persisted.status is JobStatus.RUNNING
    assert persisted.dispatch_state == "published"


def test_concurrent_duplicate_deliveries_claim_once(tmp_path) -> None:
    store = JobStorageService(LocalJsonJobRepository(tmp_path))
    store.save(_job())

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: store.claim_for_execution("job-dispatch"), range(2)))

    assert sum(result is not None for result in results) == 1
    assert store.get_internal("job-dispatch").attempt == 1


def test_duplicate_worker_delivery_claims_job_once(tmp_path) -> None:
    store = JobStorageService(LocalJsonJobRepository(tmp_path))
    store.save(_job())

    first = store.claim_for_execution("job-dispatch")
    second = store.claim_for_execution("job-dispatch")

    assert first is not None
    assert first.status is JobStatus.RUNNING
    assert first.attempt == 1
    assert second is None
    assert store.get_internal("job-dispatch").status is JobStatus.RUNNING


@pytest.mark.parametrize("status", [JobStatus.RUNNING, JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED])
def test_terminal_or_active_job_is_not_reclaimed(status: JobStatus, tmp_path) -> None:
    store = JobStorageService(LocalJsonJobRepository(tmp_path))
    store.save(_job(status=status))

    assert store.claim_for_execution("job-dispatch") is None
    assert store.get_internal("job-dispatch").status is status
