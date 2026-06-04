from __future__ import annotations

import traceback
import zipfile
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from contextlib import contextmanager
from pathlib import Path
import time
import threading
from typing import Callable, Protocol

from omicsprism.config import AnalysisConfig
from omicsprism.core import MultiOmicsEngine
from omicsprism.deg.pipeline import run_pipeline as run_deg_pipeline
from omicsprism.dem.pipeline import run_pipeline as run_dem_pipeline
from omicsprism.io import load_as_anndata, preprocess_adata
from omicsprism.utils import get_logger

from .file_service import LocalFileService
from .job_store import JobStorageService
from .models import AnalysisType, JobRecord, JobStatus
from .observability import log_context


ProgressReporter = Callable[[int, str], None]
LOG = logging.getLogger("omicsprism.platform.worker")


class JobCancelled(RuntimeError):
    pass


class JobExecutor(Protocol):
    """Submission boundary for background work.

    Replace this with a Redis/Celery/RQ-backed implementation when jobs move
    out of the API process. The API should keep calling only enqueue(job_id).
    """

    def enqueue(self, job_id: str) -> None:
        ...


class JobRunner(Protocol):
    """Runs a persisted job and owns status transitions after enqueue."""

    def run(self, job_id: str) -> None:
        ...


class LocalThreadJobExecutor:
    """Development/local executor that runs jobs in-process on one worker thread."""

    def __init__(self, runner: JobRunner, max_workers: int = 1) -> None:
        self.runner = runner
        self.pool = ThreadPoolExecutor(max_workers=max_workers)

    def enqueue(self, job_id: str) -> None:
        self.pool.submit(self.runner.run, job_id)


class QueueJobExecutor:
    """Placeholder adapter for a future queue-backed worker process.

    A Celery/RQ implementation would publish only the job id here. A separate
    worker process would consume the message, instantiate OmicsPrismJobRunner,
    and call runner.run(job_id), preserving the same job.json contract.
    """

    def enqueue(self, job_id: str) -> None:  # pragma: no cover - integration placeholder
        raise NotImplementedError("Configure a queue-backed executor such as Celery or RQ")


class RedisJobQueue:
    def __init__(self, redis_url: str, queue_name: str = "omicsprism:jobs") -> None:
        self.redis_url = redis_url
        self.queue_name = queue_name
        self._client = None

    @property
    def client(self):
        if self._client is None:
            try:
                import redis
            except ImportError as exc:  # pragma: no cover - dependency guard
                raise RuntimeError("Install redis>=5.0.0 to use Redis job queue") from exc
            self._client = redis.Redis.from_url(self.redis_url, decode_responses=True)
        return self._client

    def enqueue(self, job_id: str) -> None:
        LOG.info("enqueue job", extra={"job_id": job_id, "event": "job.enqueue"})
        self.client.rpush(self.queue_name, job_id)

    def dequeue(self, timeout_seconds: int = 5) -> str | None:
        item = self.client.blpop(self.queue_name, timeout=timeout_seconds)
        if not item:
            return None
        _queue, job_id = item
        return str(job_id)


class RedisJobExecutor:
    """Production API executor.

    The API process only persists the job and publishes the job id to Redis.
    A separate worker process consumes the id and calls OmicsPrismJobRunner.
    """

    def __init__(self, queue: RedisJobQueue) -> None:
        self.queue = queue

    def enqueue(self, job_id: str) -> None:
        LOG.info("publish job to redis", extra={"job_id": job_id, "event": "job.enqueue"})
        self.queue.enqueue(job_id)


class RedisWorker:
    def __init__(self, queue: RedisJobQueue, runner: JobRunner, *, idle_sleep_seconds: float = 0.2) -> None:
        self.queue = queue
        self.runner = runner
        self.idle_sleep_seconds = idle_sleep_seconds

    def run_forever(self) -> None:  # pragma: no cover - process entrypoint
        while True:
            job_id = self.queue.dequeue()
            if job_id is None:
                time.sleep(self.idle_sleep_seconds)
                continue
            LOG.info("dequeued job", extra={"job_id": job_id, "event": "job.dequeue"})
            self.runner.run(job_id)


class OmicsPrismJobRunner:
    def __init__(
        self,
        store: JobStorageService,
        files: LocalFileService,
        *,
        ensure_figure_specs: Callable[[str], dict[str, object]],
        remaining_seconds: Callable[[JobRecord, int], int | None],
    ) -> None:
        self.store = store
        self.files = files
        self.ensure_figure_specs = ensure_figure_specs
        self.remaining_seconds = remaining_seconds

    def run(self, job_id: str) -> None:
        job = self.store.get(job_id)
        if job.status == JobStatus.CANCELLED:
            LOG.info("skip cancelled job", extra={"job_id": job_id, "event": "job.skip_cancelled"})
            return
        paths = self.files.paths(job_id)
        input_dir = paths.input_dir
        output_dir = paths.output_dir
        output_dir.mkdir(parents=True, exist_ok=True)
        attempt = job.attempt + 1

        def report(progress: int, step: str) -> None:
            current = self.store.get(job_id)
            if current.status == JobStatus.CANCELLED:
                LOG.info("stop cancelled job at checkpoint", extra={"job_id": job_id, "event": "job.stop_cancelled"})
                raise JobCancelled("Job was cancelled by user")
            self.store.update(
                current,
                status=JobStatus.RUNNING if current.status == JobStatus.RUNNING else current.status,
                progress=progress,
                progress_step=step,
                estimated_remaining_seconds=self.remaining_seconds(current, progress),
            )
            synced = self.files.sync_workspace_artifacts(current)
            if synced:
                current = self.store.get(job_id)
                current = self.files.update_job_artifacts(current, synced)
                self.store.save(current)

        with log_context(job_id=job.id, user_id=job.owner_id or None, project_id=job.project_id or job.id):
            LOG.info("start job", extra={"event": "job.start"})
            try:
                self.store.update(
                    job,
                    status=JobStatus.RUNNING,
                    error=None,
                    started_at=datetime.now(UTC),
                    completed_at=None,
                    progress=5,
                    progress_step="Preparing data",
                    estimated_remaining_seconds=self.remaining_seconds(job, 5),
                    attempt=attempt,
                )
                paths_by_field = self.files.resolve_inputs_by_field(job)
                self._raise_if_cancelled(job_id)

                if job.analysis_type == AnalysisType.DIFFERENTIAL:
                    _run_differential_job(job, self.store, input_dir, output_dir, paths_by_field, report)
                elif job.analysis_type == AnalysisType.DEM:
                    _run_dem_job(job, self.store, input_dir, output_dir, paths_by_field, report)
                else:
                    _run_correlation_job(job, self.store, input_dir, output_dir, paths_by_field, report)

                self._raise_if_cancelled(job_id)
                self.ensure_figure_specs(job_id)
                artifacts = self.files.sync_workspace_artifacts(job)
                completed = self.store.get(job_id)
                if completed.status == JobStatus.CANCELLED:
                    LOG.info("job cancelled after analysis step", extra={"event": "job.cancelled"})
                    return
                completed = self.files.update_job_artifacts(completed, artifacts)
                self.store.update(
                    completed,
                    status=JobStatus.SUCCEEDED,
                    progress=100,
                    progress_step="Analysis complete",
                    completed_at=datetime.now(UTC),
                    estimated_remaining_seconds=0,
                    error=None,
                )
                self.store.save(completed)
                LOG.info("job succeeded", extra={"event": "job.succeeded"})
            except JobCancelled:
                cancelled = self.store.get(job_id)
                self.store.update(
                    cancelled,
                    status=JobStatus.CANCELLED,
                    progress=cancelled.progress,
                    progress_step="Cancelled by user",
                    completed_at=datetime.now(UTC),
                    estimated_remaining_seconds=0,
                )
                LOG.info("job cancelled", extra={"event": "job.cancelled"})
            except Exception as exc:  # pragma: no cover
                LOG.exception("job failed", extra={"event": "job.failed"})
                failed = self.store.get(job_id)
                output_dir.mkdir(parents=True, exist_ok=True)
                self.files.write_error_log(job_id, traceback.format_exc())
                can_retry = attempt <= failed.max_retries
                self.store.update(
                    failed,
                    status=JobStatus.QUEUED if can_retry else JobStatus.FAILED,
                    completed_at=None if can_retry else datetime.now(UTC),
                    estimated_remaining_seconds=self.remaining_seconds(failed, failed.progress) if can_retry else 0,
                    error=str(exc),
                )
                if can_retry:
                    self.run(job_id)

    def _raise_if_cancelled(self, job_id: str) -> None:
        if self.store.get(job_id).status == JobStatus.CANCELLED:
            raise JobCancelled("Job was cancelled by user")


def _run_differential_job(
    job: JobRecord,
    store: JobStorageService,
    input_dir: Path,
    output_dir: Path,
    paths_by_field: dict[str, Path],
    report: ProgressReporter,
) -> None:
    logger = get_logger(log_file=output_dir / "omicsprism.log", level="INFO")
    logger.info("Launching differential analysis job: %s", job.id)
    logger.info("Input directory: %s", input_dir.resolve())
    report(20, "Loading input data")

    same_fields = [item.strip() for item in str(job.params.get("same_fields") or "").split(",") if item.strip()]
    tested_levels = [item.strip() for item in str(job.params.get("tested_levels") or "").split(",") if item.strip()]

    report(55, "Running differential analysis")
    with _progress_heartbeat(store, job.id, start_progress=55, ceiling_progress=84, step="Running differential analysis"):
        run_deg_pipeline(
            counts_path=paths_by_field["counts"],
            metadata_path=paths_by_field["metadata"],
            out_dir=output_dir,
            same_fields=same_fields,
            compare_field=str(job.params.get("compare_field") or "group1"),
            tested_levels=tested_levels,
            reference_level=str(job.params.get("reference_level") or ""),
            padj_cutoff=float(job.params.get("padj_cutoff") or 0.05),
            log2fc_cutoff=float(job.params.get("log2fc_cutoff") or 1.0),
            min_total_count=int(job.params.get("min_total_count") or 10),
            min_replicates=int(job.params.get("min_replicates") or 2),
            n_cpus=8,
        )
    report(85, "Generating differential results")
    _zip_directory(output_dir, output_dir / "OmicsPrism_results.zip")
    report(100, "Analysis complete")


def _run_dem_job(
    job: JobRecord,
    store: JobStorageService,
    input_dir: Path,
    output_dir: Path,
    paths_by_field: dict[str, Path],
    report: ProgressReporter,
) -> None:
    logger = get_logger(log_file=output_dir / "omicsprism.log", level="INFO")
    logger.info("Launching DEM analysis job: %s", job.id)
    logger.info("Input directory: %s", input_dir.resolve())
    report(20, "Loading input data")

    same_fields = [item.strip() for item in str(job.params.get("same_fields") or "").split(",") if item.strip()]
    tested_levels = [item.strip() for item in str(job.params.get("tested_levels") or "").split(",") if item.strip()]

    report(55, "Running DEM analysis")
    with _progress_heartbeat(store, job.id, start_progress=55, ceiling_progress=84, step="Running DEM analysis"):
        run_dem_pipeline(
            metabs_path=paths_by_field["metabs"],
            metadata_path=paths_by_field["metadata"],
            out_dir=output_dir,
            same_fields=same_fields,
            compare_field=str(job.params.get("compare_field") or "group1"),
            tested_levels=tested_levels,
            reference_level=str(job.params.get("reference_level") or ""),
            vip_cutoff=float(job.params.get("vip_cutoff") or 1.0),
            padj_cutoff=float(job.params.get("padj_cutoff") or 0.05),
            log2fc_cutoff=float(job.params.get("log2fc_cutoff") or 1.0),
            pseudocount=float(job.params.get("pseudocount") or 1e-9),
            max_missing_fraction=float(job.params.get("max_missing_fraction") or 0.5),
            impute_method=str(job.params.get("impute_method") or "half-min"),
            normalize=bool(job.params.get("normalize", True)),
            log_transform=bool(job.params.get("log_transform", True)),
            min_replicates=int(job.params.get("min_replicates") or 2),
            n_orthogonal_components=int(job.params.get("n_orthogonal_components") or 1),
        )
    report(85, "Generating DEM results")
    _zip_directory(output_dir, output_dir / "OmicsPrism_results.zip")
    report(100, "Analysis complete")


def _run_correlation_job(
    job: JobRecord,
    store: JobStorageService,
    input_dir: Path,
    output_dir: Path,
    paths_by_field: dict[str, Path],
    report: ProgressReporter,
) -> None:
    group_path = paths_by_field["group"]
    cfg = AnalysisConfig(
        output_dir=str(output_dir),
        group_table_path=str(group_path),
        report_formats=("html",),
        generate_reports=True,
    )

    logger = get_logger(log_file=output_dir / "omicsprism.log", level=cfg.log_level)
    logger.info("Launching OmicsPrism platform job: %s", job.id)
    logger.info("Input directory: %s", input_dir.resolve())
    logger.info("Correlation method requested by UI: %s", job.params.get("method") or "spearman")

    report(20, "Loading input data")
    with _progress_heartbeat(store, job.id, start_progress=20, ceiling_progress=39, step="Loading input data"):
        adata = load_as_anndata(
            paths_by_field["transcriptome"],
            paths_by_field["metabolome"],
            group_table_path=cfg.group_table_path,
        )
    report(40, "Preprocessing data")
    with _progress_heartbeat(store, job.id, start_progress=40, ceiling_progress=59, step="Preprocessing data"):
        adata = preprocess_adata(
            adata,
            missing_feature_threshold=cfg.missing_feature_threshold,
            knn_neighbors=cfg.knn_neighbors,
            trans_log2=cfg.trans_log2,
        )
    report(60, "Running correlation analysis")
    with _progress_heartbeat(store, job.id, start_progress=60, ceiling_progress=84, step="Running correlation analysis"):
        engine = MultiOmicsEngine(adata, cfg)
        engine.run_all(generate_plots=cfg.generate_reports)
    report(85, "Generating visualization outputs")
    _zip_directory(output_dir, output_dir / "OmicsPrism_results.zip")
    report(100, "Analysis complete")


def _zip_directory(source_dir: Path, zip_path: Path) -> Path:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in source_dir.rglob("*"):
            if path == zip_path or not path.is_file():
                continue
            archive.write(path, path.relative_to(source_dir))
    return zip_path


@contextmanager
def _progress_heartbeat(
    store: JobStorageService,
    job_id: str,
    *,
    start_progress: int,
    ceiling_progress: int,
    step: str,
    interval_seconds: float = 15.0,
) -> None:
    stop = threading.Event()

    def _run() -> None:
        while not stop.wait(interval_seconds):
            try:
                current = store.get(job_id)
            except Exception:
                LOG.warning("heartbeat failed to read job", extra={"job_id": job_id}, exc_info=True)
                continue
            if current.status != JobStatus.RUNNING:
                continue
            next_progress = min(ceiling_progress, max(current.progress + 1, start_progress + 1))
            remaining = current.estimated_remaining_seconds
            if remaining is not None:
                remaining = max(0, remaining - int(interval_seconds))
            store.update(
                current,
                status=JobStatus.RUNNING,
                progress=next_progress,
                progress_step=step,
                estimated_remaining_seconds=remaining,
            )

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=5.0)




