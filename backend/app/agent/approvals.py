from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4
from typing import Any, Protocol

from .schemas import ApprovalRecord, ApprovalStatus


class ApprovalGate(Protocol):
    """审批生命周期接口；Phase 0 不执行任何写副作用。"""

    def suspend(self, *, run_id: str, user_id: str, plan_hash: str, expires_at: datetime) -> str:
        ...

    def resume(self, *, approval_id: str, run_id: str, user_id: str, plan_hash: str,
               now: datetime | None = None) -> None:
        ...

    def is_valid(self, *, approval_id: str, run_id: str, user_id: str, plan_hash: str,
                 now: datetime | None = None) -> bool:
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

    def suspend(self, *, run_id: str, user_id: str, plan_hash: str, expires_at: datetime) -> str:
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
        if record.run_id != run_id or record.user_id != user_id or record.plan_hash != plan_hash:
            raise ApprovalMismatch("approval does not match run, user, or plan")
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


class JsonApprovalGate:
    """审批记录的原子 JSON 实现，供单机 API/worker 部署跨进程重建。"""

    def __init__(self, root: Path) -> None:
        self.root = root

    def suspend(self, *, run_id: str, user_id: str, plan_hash: str, expires_at: datetime) -> str:
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
        if record.run_id != run_id or record.user_id != user_id or record.plan_hash != plan_hash:
            raise ApprovalMismatch("approval does not match run, user, or plan")
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
