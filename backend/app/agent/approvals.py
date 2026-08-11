from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4
from typing import Any, Protocol

from .schemas import ApprovalRecord, ApprovalStatus


class ApprovalGate(Protocol):
    """审批生命周期接口；Phase 0 不执行任何写副作用。"""

    def suspend(self, *, run_id: str, user_id: str, plan_hash: str, expires_at: datetime,
                plan_id: str | None = None, thread_id: str | None = None) -> str:
        ...

    def resume(self, *, approval_id: str, run_id: str, user_id: str, plan_hash: str,
               now: datetime | None = None) -> None:
        ...

    def is_valid(self, *, approval_id: str, run_id: str, user_id: str, plan_hash: str,
                 now: datetime | None = None) -> bool:
        ...

    def reject(self, *, approval_id: str, run_id: str, user_id: str, plan_hash: str,
               now: datetime | None = None) -> None:
        ...

    def get_owned(self, *, approval_id: str, user_id: str) -> ApprovalRecord:
        ...


class ApprovalNotFound(LookupError):
    pass


class ApprovalMismatch(PermissionError):
    pass


class ApprovalExpired(PermissionError):
    pass


class InMemoryApprovalGate:
    def __init__(self, shared: dict[str, Any] | None = None) -> None:
        self._records = shared if shared is not None else {}

    def suspend(self, *, run_id: str, user_id: str, plan_hash: str, expires_at: datetime,
                plan_id: str | None = None, thread_id: str | None = None) -> str:
        approval_id = f"approval-{uuid4()}"
        record = ApprovalRecord(
            approval_id=approval_id, run_id=run_id, user_id=user_id,
            plan_hash=plan_hash, status=ApprovalStatus.PENDING, expires_at=expires_at,
        )
        self._records[approval_id] = record.model_dump(mode="json")
        return approval_id

    def resume(self, *, approval_id: str, run_id: str, user_id: str, plan_hash: str,
               now: datetime | None = None) -> None:
        payload = self._records.get(approval_id)
        if payload is None:
            raise ApprovalNotFound(approval_id)
        record = ApprovalRecord.model_validate(payload)
        if record.user_id != user_id:
            raise ApprovalNotFound(approval_id)
        if record.run_id != run_id or record.plan_hash != plan_hash:
            raise ApprovalMismatch("approval does not match run, user, or plan")
        if record.status is ApprovalStatus.REJECTED:
            raise ApprovalMismatch("rejected approval cannot be resumed")
        if record.status is ApprovalStatus.EXPIRED:
            raise ApprovalExpired(approval_id)
        current = now or datetime.now(timezone.utc)
        if current >= record.expires_at:
            record.status = ApprovalStatus.EXPIRED
            self._records[approval_id] = record.model_dump(mode="json")
            raise ApprovalExpired(approval_id)
        record.status = ApprovalStatus.APPROVED
        self._records[approval_id] = record.model_dump(mode="json")

    def is_valid(self, *, approval_id: str, run_id: str, user_id: str, plan_hash: str,
                 now: datetime | None = None) -> bool:
        payload = self._records.get(approval_id)
        if payload is None:
            return False
        record = ApprovalRecord.model_validate(payload)
        if record.status is not ApprovalStatus.APPROVED:
            return False
        if record.run_id != run_id or record.user_id != user_id or record.plan_hash != plan_hash:
            return False
        current = now or datetime.now(timezone.utc)
        return current < record.expires_at

    def reject(self, *, approval_id: str, run_id: str, user_id: str, plan_hash: str,
               now: datetime | None = None) -> None:
        record = self._owned_record(approval_id, user_id)
        if record.run_id != run_id or record.plan_hash != plan_hash:
            raise ApprovalMismatch("approval does not match run, user, or plan")
        if record.status is ApprovalStatus.REJECTED:
            raise ApprovalMismatch("rejected approval cannot be resumed")
        if record.status is ApprovalStatus.EXPIRED:
            raise ApprovalExpired(approval_id)
        current = now or datetime.now(timezone.utc)
        if current >= record.expires_at:
            record.status = ApprovalStatus.EXPIRED
            self._records[approval_id] = record.model_dump(mode="json")
            raise ApprovalExpired(approval_id)
        record.status = ApprovalStatus.REJECTED
        self._records[approval_id] = record.model_dump(mode="json")

    def get_owned(self, *, approval_id: str, user_id: str) -> ApprovalRecord:
        return self._owned_record(approval_id, user_id)

    def _owned_record(self, approval_id: str, user_id: str) -> ApprovalRecord:
        payload = self._records.get(approval_id)
        if payload is None:
            raise ApprovalNotFound(approval_id)
        record = ApprovalRecord.model_validate(payload)
        if record.user_id != user_id:
            raise ApprovalNotFound(approval_id)
        return record


class JsonApprovalGate:
    """审批记录的原子 JSON 实现，供单机 API/worker 部署跨进程重建。"""

    def __init__(self, root: Path) -> None:
        self.root = root

    def suspend(self, *, run_id: str, user_id: str, plan_hash: str, expires_at: datetime,
                plan_id: str | None = None, thread_id: str | None = None) -> str:
        approval_id = f"approval-{uuid4()}"
        self._save(ApprovalRecord(
            approval_id=approval_id,
            run_id=run_id,
            user_id=user_id,
            plan_hash=plan_hash,
            status=ApprovalStatus.PENDING,
            expires_at=expires_at,
        ))
        return approval_id

    def resume(self, *, approval_id: str, run_id: str, user_id: str, plan_hash: str,
               now: datetime | None = None) -> None:
        record = self._get(approval_id)
        if record.user_id != user_id:
            raise ApprovalNotFound(approval_id)
        if record.run_id != run_id or record.plan_hash != plan_hash:
            raise ApprovalMismatch("approval does not match run, user, or plan")
        if record.status is ApprovalStatus.REJECTED:
            raise ApprovalMismatch("rejected approval cannot be resumed")
        if record.status is ApprovalStatus.EXPIRED:
            raise ApprovalExpired(approval_id)
        current = now or datetime.now(timezone.utc)
        if current >= record.expires_at:
            record.status = ApprovalStatus.EXPIRED
            self._save(record)
            raise ApprovalExpired(approval_id)
        record.status = ApprovalStatus.APPROVED
        self._save(record)

    def is_valid(self, *, approval_id: str, run_id: str, user_id: str, plan_hash: str,
                 now: datetime | None = None) -> bool:
        try:
            record = self._get(approval_id)
        except ApprovalNotFound:
            return False
        current = now or datetime.now(timezone.utc)
        return (
            record.status is ApprovalStatus.APPROVED
            and record.run_id == run_id
            and record.user_id == user_id
            and record.plan_hash == plan_hash
            and current < record.expires_at
        )

    def reject(self, *, approval_id: str, run_id: str, user_id: str, plan_hash: str,
               now: datetime | None = None) -> None:
        record = self._get(approval_id)
        if record.user_id != user_id:
            raise ApprovalNotFound(approval_id)
        if record.run_id != run_id or record.plan_hash != plan_hash:
            raise ApprovalMismatch("approval does not match run, user, or plan")
        if record.status is ApprovalStatus.REJECTED:
            raise ApprovalMismatch("rejected approval cannot be resumed")
        if record.status is ApprovalStatus.EXPIRED:
            raise ApprovalExpired(approval_id)
        current = now or datetime.now(timezone.utc)
        if current >= record.expires_at:
            record.status = ApprovalStatus.EXPIRED
            self._save(record)
            raise ApprovalExpired(approval_id)
        record.status = ApprovalStatus.REJECTED
        self._save(record)

    def get_owned(self, *, approval_id: str, user_id: str) -> ApprovalRecord:
        record = self._get(approval_id)
        if record.user_id != user_id:
            raise ApprovalNotFound(approval_id)
        return record

    def _get(self, approval_id: str) -> ApprovalRecord:
        path = self._path(approval_id)
        if not path.exists():
            raise ApprovalNotFound(approval_id)
        return ApprovalRecord.model_validate(json.loads(path.read_text(encoding="utf-8")))

    def _save(self, record: ApprovalRecord) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(record.approval_id)
        temp = path.with_suffix(".json.tmp")
        temp.write_text(record.model_dump_json(indent=2), encoding="utf-8")
        temp.replace(path)

    def _path(self, approval_id: str) -> Path:
        safe = Path(approval_id).name
        if safe != approval_id or not safe:
            raise ApprovalNotFound(approval_id)
        return self.root / f"{safe}.json"


class PostgresApprovalGate:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url

    def suspend(self, *, run_id: str, user_id: str, plan_hash: str, expires_at: datetime,
                plan_id: str | None = None, thread_id: str | None = None) -> str:
        if not plan_id or not thread_id:
            raise ValueError("PostgreSQL approval requires plan_id and thread_id")
        approval_id = f"approval-{uuid4()}"
        ttl_seconds = _approval_ttl_seconds(expires_at)
        with self._connect() as conn:
            conn.execute(
                """
                insert into agent_approvals (
                    approval_id, plan_id, run_id, thread_id, user_id,
                    plan_hash, status, expires_at
                ) values (
                    %s, %s, %s, %s, %s, %s, 'pending',
                    clock_timestamp() + (%s * interval '1 second')
                )
                """,
                (approval_id, plan_id, run_id, thread_id, user_id, plan_hash, ttl_seconds),
            )
        return approval_id

    def resume(self, *, approval_id: str, run_id: str, user_id: str, plan_hash: str,
               now: datetime | None = None) -> None:
        with self._connect() as conn:
            current = self._database_now(conn)
            record = self._get_owned(conn, approval_id, user_id)
            self._validate_binding(record, run_id, plan_hash)
            if record.status is ApprovalStatus.REJECTED:
                raise ApprovalMismatch("rejected approval cannot be resumed")
            if record.status is ApprovalStatus.EXPIRED:
                raise ApprovalExpired(approval_id)
            if current >= record.expires_at:
                conn.execute(
                    "update agent_approvals set status = 'expired', updated_at = now() where approval_id = %s and user_id = %s",
                    (approval_id, user_id),
                )
                conn.commit()
                raise ApprovalExpired(approval_id)
            conn.execute(
                "update agent_approvals set status = 'approved', updated_at = now() where approval_id = %s and user_id = %s",
                (approval_id, user_id),
            )

    def is_valid(self, *, approval_id: str, run_id: str, user_id: str, plan_hash: str,
                 now: datetime | None = None) -> bool:
        try:
            with self._connect() as conn:
                current = self._database_now(conn)
                record = self._get_owned(conn, approval_id, user_id)
        except ApprovalNotFound:
            return False
        return (
            record.run_id == run_id
            and record.plan_hash == plan_hash
            and record.status is ApprovalStatus.APPROVED
            and current < record.expires_at
        )

    def reject(self, *, approval_id: str, run_id: str, user_id: str, plan_hash: str,
               now: datetime | None = None) -> None:
        with self._connect() as conn:
            current = self._database_now(conn)
            record = self._get_owned(conn, approval_id, user_id)
            self._validate_binding(record, run_id, plan_hash)
            if record.status is ApprovalStatus.REJECTED:
                raise ApprovalMismatch("approval is already rejected")
            if record.status is ApprovalStatus.EXPIRED:
                raise ApprovalExpired(approval_id)
            if current >= record.expires_at:
                conn.execute(
                    "update agent_approvals set status = 'expired', updated_at = now() where approval_id = %s and user_id = %s",
                    (approval_id, user_id),
                )
                conn.commit()
                raise ApprovalExpired(approval_id)
            conn.execute(
                "update agent_approvals set status = 'rejected', updated_at = now() where approval_id = %s and user_id = %s",
                (approval_id, user_id),
            )

    def get_owned(self, *, approval_id: str, user_id: str) -> ApprovalRecord:
        with self._connect() as conn:
            return self._get_owned(conn, approval_id, user_id)

    @staticmethod
    def _get_owned(conn, approval_id: str, user_id: str) -> ApprovalRecord:
        row = conn.execute(
            """
            select approval_id, run_id, user_id, plan_hash, status, expires_at
            from agent_approvals where approval_id = %s and user_id = %s
            """,
            (approval_id, user_id),
        ).fetchone()
        if row is None:
            raise ApprovalNotFound(approval_id)
        fields = ("approval_id", "run_id", "user_id", "plan_hash", "status", "expires_at")
        return ApprovalRecord.model_validate(dict(zip(fields, row)))

    @staticmethod
    def _validate_binding(record: ApprovalRecord, run_id: str, plan_hash: str) -> None:
        if record.run_id != run_id or record.plan_hash != plan_hash:
            raise ApprovalMismatch("approval does not match run, user, or plan")

    @staticmethod
    def _database_now(conn) -> datetime:
        return conn.execute("select clock_timestamp()").fetchone()[0]

    def _connect(self):
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise RuntimeError("Install psycopg[binary]>=3.1.18 to use PostgreSQL storage") from exc
        return psycopg.connect(self.database_url)


def _approval_ttl_seconds(expires_at: datetime) -> int:
    """把调用方的绝对时间转换为相对 TTL，实际到期点由 PostgreSQL 生成。"""
    current = datetime.now(timezone.utc)
    normalized = expires_at if expires_at.tzinfo is not None else expires_at.replace(tzinfo=timezone.utc)
    return max(1, min(24 * 60 * 60, round((normalized - current).total_seconds())))
