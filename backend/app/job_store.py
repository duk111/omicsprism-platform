from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from fastapi import HTTPException

from .models import JobRecord, JobStatus
from .observability import AuditService, audit_job_status


class JobRepository(Protocol):
    def get(self, job_id: str, user_id: str) -> JobRecord:
        ...

    def get_internal(self, job_id: str) -> JobRecord:
        ...

    def save(self, job: JobRecord) -> None:
        ...

    def list_for_user(self, user_id: str, include_deleted: bool = False) -> list[JobRecord]:
        ...

    def list_internal(self, include_deleted: bool = False) -> list[JobRecord]:
        ...


class LocalJsonJobRepository:
    """Local JSON-backed repository; replace this for a DB-backed implementation."""

    def __init__(self, runs_dir: Path) -> None:
        self.runs_dir = runs_dir

    def get(self, job_id: str, user_id: str) -> JobRecord:
        job = self.get_internal(job_id)
        if job.owner_id != user_id:
            raise HTTPException(status_code=404, detail="Job not found")
        return job

    def get_internal(self, job_id: str) -> JobRecord:
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

    def list_for_user(self, user_id: str, include_deleted: bool = False) -> list[JobRecord]:
        return [job for job in self.list_internal(include_deleted=include_deleted) if job.owner_id == user_id]

    def list_internal(self, include_deleted: bool = False) -> list[JobRecord]:
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


class PostgresJobRepository:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    def get(self, job_id: str, user_id: str) -> JobRecord:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "select payload from jobs where id = %s and owner_id = %s",
                    (job_id, user_id),
                )
                row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return JobRecord.model_validate(row[0])

    def get_internal(self, job_id: str) -> JobRecord:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute("select payload from jobs where id = %s", (job_id,))
                row = cur.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return JobRecord.model_validate(row[0])

    def save(self, job: JobRecord) -> None:
        try:
            from psycopg.types.json import Jsonb
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise RuntimeError("Install psycopg[binary]>=3.1.18 to use PostgreSQL storage") from exc
        payload = job.model_dump(mode="json")
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into jobs (
                        id, project_id, owner_type, owner_id, status, analysis_type,
                        created_at, updated_at, deleted_at, payload
                    )
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    on conflict (id) do update set
                        project_id = excluded.project_id,
                        owner_type = excluded.owner_type,
                        owner_id = excluded.owner_id,
                        status = excluded.status,
                        analysis_type = excluded.analysis_type,
                        updated_at = excluded.updated_at,
                        deleted_at = excluded.deleted_at,
                        payload = excluded.payload
                    """,
                    (
                        job.id,
                        job.project_id,
                        str(job.owner_type.value),
                        job.owner_id,
                        str(job.status.value),
                        str(job.analysis_type.value),
                        job.created_at,
                        job.updated_at,
                        job.deleted_at,
                        Jsonb(payload),
                    ),
                )
                if job.status in {
                    JobStatus.SUCCEEDED,
                    JobStatus.FAILED,
                    JobStatus.CANCELLED,
                }:
                    # The Job row and its completion event commit together.
                    # Jobs without an Agent wait simply produce no event.
                    cur.execute(
                        """
                        insert into agent_job_events (
                            event_id, event_type, job_id, thread_id, user_id,
                            turn_id, run_id, trace_id, status, error_code,
                            attempt, occurred_at
                        )
                        select
                            'job-completion:' || j.id || ':' || j.status,
                            'job.completed', j.id, w.thread_id, w.user_id,
                            w.turn_id, w.run_id, w.trace_id, j.status,
                            case j.status
                                when 'failed' then 'job_failed'
                                when 'cancelled' then 'job_cancelled'
                                else null
                            end,
                            coalesce((j.payload ->> 'attempt')::integer, 0),
                            j.updated_at
                        from jobs j
                        join agent_job_waits w
                          on w.job_id = j.id
                         and w.user_id = j.owner_id
                        where j.id = %s
                          and j.status in ('succeeded', 'failed', 'cancelled')
                          and w.status = 'waiting'
                        on conflict (event_id) do nothing
                        """,
                        (job.id,),
                    )

    def list_for_user(self, user_id: str, include_deleted: bool = False) -> list[JobRecord]:
        deleted_clause = "" if include_deleted else "and deleted_at is null"
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"select payload from jobs where owner_id = %s {deleted_clause} order by created_at desc",
                    (user_id,),
                )
                rows = cur.fetchall()
        return [JobRecord.model_validate(row[0]) for row in rows]

    def list_internal(self, include_deleted: bool = False) -> list[JobRecord]:
        where = "" if include_deleted else "where deleted_at is null"
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"select payload from jobs {where} order by created_at desc")
                rows = cur.fetchall()
        return [JobRecord.model_validate(row[0]) for row in rows]

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise RuntimeError("Install psycopg[binary]>=3.1.18 to use PostgreSQL storage") from exc
        return psycopg.connect(self.database_url)

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

    def get_for_user(self, job_id: str, user_id: str) -> JobRecord:
        return self.repository.get(job_id, user_id)

    def get_internal(self, job_id: str) -> JobRecord:
        return self.repository.get_internal(job_id)

    def save(self, job: JobRecord) -> None:
        with self._job_lock(job.id):
            previous_status = self._previous_status(job.id)
            self.repository.save(job)
        audit_job_status(self.audit, job=job, previous_status=previous_status)

    def list_for_user(self, user_id: str, include_deleted: bool = False) -> list[JobRecord]:
        return self.repository.list_for_user(user_id, include_deleted=include_deleted)

    def list_internal(self, include_deleted: bool = False) -> list[JobRecord]:
        return self.repository.list_internal(include_deleted=include_deleted)

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
            job.updated_at = datetime.now(timezone.utc)
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
            return self.repository.get_internal(job_id).status
        except Exception:
            return None
