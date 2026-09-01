from __future__ import annotations

from backend.app.bootstrap import create_context
from backend.app.job_execution import RedisJobQueue, RedisWorker
from backend.app.main import _ensure_figure_specs, _remaining_seconds
from backend.app.observability import configure_logging
from backend.app.settings import load_settings


def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    if settings.executor_backend != "redis":
        raise SystemExit("Worker requires OMICS_PRISM_EXECUTOR=redis")

    context = create_context(
        settings,
        ensure_figure_specs=lambda job_id: _ensure_figure_specs(job_id),
        remaining_seconds=lambda job, progress: _remaining_seconds(job, progress),
        include_executor=False,
    )
    queue = context.redis_queue or RedisJobQueue(settings.redis_url, settings.redis_queue_name)
    RedisWorker(
        queue,
        context.job_runner,
        job_store=context.job_store,
        job_timeout_seconds=settings.job_timeout_seconds,
    ).run_forever()


if __name__ == "__main__":
    main()
