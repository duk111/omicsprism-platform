from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta

from backend.app.bootstrap import create_context
from backend.app.models import JobStatus
from backend.app.observability import configure_logging
from backend.app.settings import load_settings


LOG = logging.getLogger("omicsprism.platform.housekeeping")
TERMINAL_STATUSES = {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}


def cleanup_once() -> int:
    settings = load_settings()
    configure_logging(settings.log_level)
    context = create_context(
        settings,
        ensure_figure_specs=lambda job_id: {},
        remaining_seconds=lambda job, progress: None,
        include_executor=False,
    )
    cutoff = datetime.now(UTC) - timedelta(hours=settings.job_history_ttl_hours)
    removed = 0

    for job in context.job_store.list_internal(include_deleted=False):
        if job.status not in TERMINAL_STATUSES:
            continue
        reference_time = job.completed_at or job.updated_at or job.created_at
        if reference_time > cutoff:
            continue
        try:
            context.files.cleanup_job_storage(job)
            job.deleted_at = datetime.now(UTC)
            job.updated_at = job.deleted_at
            context.job_store.save(job)
            removed += 1
            LOG.info("expired job cleaned", extra={"job_id": job.id})
        except Exception:
            LOG.exception("failed to clean expired job", extra={"job_id": job.id})

    LOG.info("housekeeping pass complete", extra={"removed_jobs": removed})
    return removed


def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    interval = max(60, settings.housekeeping_interval_seconds)
    while True:
        cleanup_once()
        time.sleep(interval)


if __name__ == "__main__":
    main()
