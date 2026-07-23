from __future__ import annotations

from dataclasses import dataclass

from .job_execution import JobExecutor, LocalThreadJobExecutor, OmicsPrismJobRunner, RedisJobExecutor, RedisJobQueue
from .job_store import JobStorageService, LocalJsonJobRepository, PostgresJobRepository
from .storage_service import FileStorageService
from .settings import AppSettings


@dataclass
class AppContext:
    settings: AppSettings
    files: FileStorageService
    job_store: JobStorageService
    job_runner: OmicsPrismJobRunner
    job_executor: JobExecutor | None
    redis_queue: RedisJobQueue | None = None


def create_context(
    settings: AppSettings,
    *,
    ensure_figure_specs,
    remaining_seconds,
    include_executor: bool = True,
) -> AppContext:
    files = FileStorageService(settings)

    if settings.storage_backend == "postgres":
        if not settings.runtime_database_url:
            raise RuntimeError(
                "OMICS_PRISM_RUNTIME_DATABASE_URL is required when OMICS_PRISM_STORAGE_BACKEND=postgres"
            )
        job_repository = PostgresJobRepository(settings.runtime_database_url)
    else:
        job_repository = LocalJsonJobRepository(settings.runs_dir)
    job_store = JobStorageService(job_repository)
    files.attach_job_store(job_store)

    job_runner = OmicsPrismJobRunner(
        job_store,
        files,
        ensure_figure_specs=ensure_figure_specs,
        remaining_seconds=remaining_seconds,
    )

    redis_queue = None
    if settings.executor_backend == "redis":
        redis_queue = RedisJobQueue(settings.redis_url, settings.redis_queue_name)
        job_executor = RedisJobExecutor(redis_queue) if include_executor else None
    else:
        job_executor = LocalThreadJobExecutor(job_runner, max_workers=1) if include_executor else None

    return AppContext(
        settings=settings,
        files=files,
        job_store=job_store,
        job_runner=job_runner,
        job_executor=job_executor,
        redis_queue=redis_queue,
    )
