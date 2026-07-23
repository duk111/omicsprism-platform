from __future__ import annotations

from datetime import datetime
from typing import Protocol


class ApprovalGate(Protocol):
    """审批生命周期接口；Phase 0 不执行任何写副作用。"""

    def suspend(self, *, run_id: str, user_id: str, plan_hash: str, expires_at: datetime) -> str:
        ...

    def resume(self, *, approval_id: str, run_id: str, user_id: str, plan_hash: str) -> None:
        ...
