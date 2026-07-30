from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class AppSettings:
    base_dir: Path = BASE_DIR
    runs_dir: Path = BASE_DIR / "runs"
    auth_dir: Path = BASE_DIR / "auth_data"
    storage_backend: str = "json"
    file_storage_backend: str = "local"
    file_storage_root: Path = BASE_DIR / "storage"
    file_storage_bucket: str = "omicsprism"
    file_storage_prefix: str = "jobs"
    file_storage_endpoint_url: str | None = None
    file_storage_region: str = "us-east-1"
    file_storage_access_key_id: str | None = None
    file_storage_secret_access_key: str | None = None
    file_storage_public_base_url: str | None = None
    file_storage_presign_expiry_seconds: int = 3600
    file_storage_cleanup_grace_days: int = 7
    file_storage_job_ttl_days: int = 30
    file_storage_failed_job_ttl_days: int = 14
    file_storage_temp_ttl_hours: int = 24
    job_history_ttl_hours: int = 24
    housekeeping_interval_seconds: int = 3600
    file_storage_dedupe_enabled: bool = True
    file_storage_quota_bytes: int | None = None
    max_concurrent_jobs_per_user: int | None = 2
    max_concurrent_jobs_per_project: int | None = 1
    executor_backend: str = "local"
    runtime_database_url: str | None = None
    redis_url: str = "redis://localhost:6379/0"
    redis_queue_name: str = "omicsprism:jobs"
    agent_model_url: str | None = None
    agent_model_name: str | None = None
    agent_model_api_key: str | None = None
    agent_turn_timeout_seconds: float = 90.0
    agent_lease_seconds: int = 120
    agent_max_attempts: int = 3
    agent_poll_seconds: float = 1.0
    log_level: str = "INFO"
    dev_email: str = "dev@omicsprism.local"
    dev_password: str = "dev-password"


def load_settings() -> AppSettings:
    base_dir = BASE_DIR
    return AppSettings(
        base_dir=base_dir,
        runs_dir=Path(os.getenv("OMICS_PRISM_RUNS_DIR", str(base_dir / "runs"))),
        auth_dir=Path(os.getenv("OMICS_PRISM_AUTH_DIR", str(base_dir / "auth_data"))),
        storage_backend=os.getenv("OMICS_PRISM_STORAGE_BACKEND", "json").lower(),
        file_storage_backend=os.getenv("OMICS_PRISM_FILE_STORAGE_BACKEND", "local").lower(),
        file_storage_root=Path(os.getenv("OMICS_PRISM_FILE_STORAGE_ROOT", str(base_dir / "storage"))),
        file_storage_bucket=os.getenv("OMICS_PRISM_FILE_STORAGE_BUCKET", "omicsprism"),
        file_storage_prefix=os.getenv("OMICS_PRISM_FILE_STORAGE_PREFIX", "jobs").strip("/"),
        file_storage_endpoint_url=os.getenv("OMICS_PRISM_S3_ENDPOINT_URL") or None,
        file_storage_region=os.getenv("OMICS_PRISM_S3_REGION", "us-east-1"),
        file_storage_access_key_id=os.getenv("OMICS_PRISM_S3_ACCESS_KEY_ID") or None,
        file_storage_secret_access_key=os.getenv("OMICS_PRISM_S3_SECRET_ACCESS_KEY") or None,
        file_storage_public_base_url=os.getenv("OMICS_PRISM_FILE_STORAGE_PUBLIC_BASE_URL") or None,
        file_storage_presign_expiry_seconds=int(os.getenv("OMICS_PRISM_FILE_STORAGE_PRESIGN_EXPIRY_SECONDS", "3600")),
        file_storage_cleanup_grace_days=int(os.getenv("OMICS_PRISM_FILE_STORAGE_CLEANUP_GRACE_DAYS", "7")),
        file_storage_job_ttl_days=int(os.getenv("OMICS_PRISM_FILE_STORAGE_JOB_TTL_DAYS", "30")),
        file_storage_failed_job_ttl_days=int(os.getenv("OMICS_PRISM_FILE_STORAGE_FAILED_JOB_TTL_DAYS", "14")),
        file_storage_temp_ttl_hours=int(os.getenv("OMICS_PRISM_FILE_STORAGE_TEMP_TTL_HOURS", "24")),
        job_history_ttl_hours=int(os.getenv("OMICS_PRISM_JOB_HISTORY_TTL_HOURS", "24")),
        housekeeping_interval_seconds=int(os.getenv("OMICS_PRISM_HOUSEKEEPING_INTERVAL_SECONDS", "3600")),
        file_storage_dedupe_enabled=os.getenv("OMICS_PRISM_FILE_STORAGE_DEDUPE", "true").lower() not in {"0", "false", "no"},
        file_storage_quota_bytes=_parse_optional_int(os.getenv("OMICS_PRISM_FILE_STORAGE_QUOTA_BYTES")),
        max_concurrent_jobs_per_user=_parse_optional_int(os.getenv("OMICS_PRISM_MAX_CONCURRENT_JOBS_PER_USER"), default=2),
        max_concurrent_jobs_per_project=_parse_optional_int(os.getenv("OMICS_PRISM_MAX_CONCURRENT_JOBS_PER_PROJECT"), default=1),
        executor_backend=os.getenv("OMICS_PRISM_EXECUTOR", "local").lower(),
        runtime_database_url=os.getenv("OMICS_PRISM_RUNTIME_DATABASE_URL") or None,
        redis_url=os.getenv("OMICS_PRISM_REDIS_URL", "redis://localhost:6379/0"),
        redis_queue_name=os.getenv("OMICS_PRISM_REDIS_QUEUE", "omicsprism:jobs"),
        agent_model_url=os.getenv("OMICS_PRISM_AGENT_MODEL_URL") or None,
        agent_model_name=os.getenv("OMICS_PRISM_AGENT_MODEL_NAME") or None,
        agent_model_api_key=os.getenv("OMICS_PRISM_AGENT_MODEL_API_KEY") or None,
        agent_turn_timeout_seconds=float(os.getenv("OMICS_PRISM_AGENT_TURN_TIMEOUT_SECONDS", "90")),
        agent_lease_seconds=int(os.getenv("OMICS_PRISM_AGENT_LEASE_SECONDS", "120")),
        agent_max_attempts=int(os.getenv("OMICS_PRISM_AGENT_MAX_ATTEMPTS", "3")),
        agent_poll_seconds=float(os.getenv("OMICS_PRISM_AGENT_POLL_SECONDS", "1")),
        log_level=os.getenv("OMICS_PRISM_LOG_LEVEL", "INFO"),
        dev_email=os.getenv("OMICS_PRISM_DEV_EMAIL", "dev@omicsprism.local"),
        dev_password=os.getenv("OMICS_PRISM_DEV_PASSWORD", "dev-password"),
    )


def _parse_optional_int(value: str | None, default: int | None = None) -> int | None:
    if value is None or not value.strip():
        return default
    return int(value)
