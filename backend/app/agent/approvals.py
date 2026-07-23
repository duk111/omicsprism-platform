from __future__ import annotations

from datetime import datetime, timezone
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
