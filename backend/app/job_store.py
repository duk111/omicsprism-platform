from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from fastapi import HTTPException

from .models import JobRecord, JobStatus
from .observability import AuditService, audit_job_status


class JobRepository(Protocol):
    def get(self, job_id: str) -> JobRecord:
        ...

    def save(self, job: JobRecord) -> None:
        ...

    def list(self, include_deleted: bool = False) -> list[JobRecord]:
        ...


class LocalJsonJobRepository:
    """Local JSON-backed repository; replace this for a DB-backed implementation."""

    def __init__(self, runs_dir: Path) -> None:
        self.runs_dir = runs_dir

    def get(self, job_id: str) -> JobRecord:
        path = self._job_path(job_id)
        if not path.exists():
            raise HTTPException(status_code=404, detail="Job not found")
        payload = self._load_payload(path)
        return JobRecord.model_validate(payload)

    def save(self, job: JobRecord) -> None:
        path = self._job_path(job.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(".json.tmp")
        temp_path.write_text(job.model_dump_json(indent=2), encoding="utf-8")
        temp_path.replace(path)

    def list(self, include_deleted: bool = False) -> list[JobRecord]:
        if not self.runs_dir.exists():
            return []

        jobs: list[JobRecord] = []
        for path in self.runs_dir.glob("*/job.json"):
            try:
                job = JobRecord.model_validate(self._load_payload(path))
                if not include_deleted and job.deleted_at is not None:
                    continue
                jobs.append(job)
            except Exception:
                continue
        jobs.sort(key=lambda item: item.created_at, reverse=True)
        return jobs

    def _job_path(self, job_id: str) -> Path:
        return self.runs_dir / job_id / "job.json"

    def _load_payload(self, path: Path) -> dict[str, object]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Job record must be a JSON object")
        normalized, changed = self._normalize_legacy_payload(payload)
        if changed:
            self._write_payload(path, normalized)
        return normalized

    def _normalize_legacy_payload(self, payload: dict[str, object]) -> tuple[dict[str, object], bool]:
        changed = False
        raw_inputs = payload.get("inputs")
        if isinstance(raw_inputs, list):
            for item in raw_inputs:
                if not isinstance(item, dict):
                    continue
                field = item.get("field")
                if isinstance(field, str) and field.strip():
                    continue
                inferred = self._infer_input_field(item)
                if inferred:
                    item["field"] = inferred
                    changed = True
        return payload, changed

    def _infer_input_field(self, item: dict[str, object]) -> str | None:
        for key in ("path", "filename"):
            candidate = item.get(key)
            if isinstance(candidate, str) and candidate.strip():
                stem = Path(candidate).stem.strip()
                if stem:
                    return stem
        return None

    def _write_payload(self, path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(".json.tmp")
        temp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        temp_path.replace(path)


class JobStorageService:
    def __init__(self, repository: JobRepository, audit: AuditService | None = None) -> None:
        self.repository = repository
        self.audit = audit
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _job_lock(self, job_id: str) -> threading.Lock:
        with self._locks_guard:
            if job_id not in self._locks:
                self._locks[job_id] = threading.Lock()
            return self._locks[job_id]

    def get(self, job_id: str) -> JobRecord:
        return self.repository.get(job_id)

    def save(self, job: JobRecord) -> None:
        with self._job_lock(job.id):
            previous_status = self._previous_status(job.id)
            self.repository.save(job)
        audit_job_status(self.audit, job=job, previous_status=previous_status)

    def list(self, include_deleted: bool = False) -> list[JobRecord]:
        return self.repository.list(include_deleted=include_deleted)

    def update(
        self,
        job: JobRecord,
        *,
        status: JobStatus,
        progress: int | None = None,
        progress_step: str | None = None,
        error: str | None = None,
        started_at: datetime | None = None,
        completed_at: datetime | None = None,
        estimated_remaining_seconds: int | None = None,
        attempt: int | None = None,
    ) -> None:
        with self._job_lock(job.id):
            job.status = status
            job.updated_at = datetime.now(UTC)
            if progress is not None:
                job.progress = progress
            if progress_step is not None:
                job.progress_step = progress_step
            if error is not None or status == JobStatus.SUCCEEDED:
                job.error = error
            if started_at is not None:
                job.started_at = started_at
            if completed_at is not None:
                job.completed_at = completed_at
            if estimated_remaining_seconds is not None:
                job.estimated_remaining_seconds = estimated_remaining_seconds
            if attempt is not None:
                job.attempt = attempt
            previous_status = self._previous_status(job.id)
            self.repository.save(job)
        audit_job_status(self.audit, job=job, previous_status=previous_status)

    def _previous_status(self, job_id: str) -> JobStatus | None:
        try:
            return self.repository.get(job_id).status
        except Exception:
            return None
