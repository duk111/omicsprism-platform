from __future__ import annotations

from dataclasses import dataclass

from ..job_store import JobStorageService
from ..settings import AppSettings
from ..storage_service import FileStorageService
from .approvals import ApprovalGate, PostgresApprovalGate
from .plans import PlanStore, PostgresPlanStore
from .product_store import AgentProductStore, PostgresAgentProductStore
from .store import PostgresStateStore, StateStore


@dataclass(frozen=True)
class AgentApiContext:
    """API 进程只持有持久化与文件适配器，不持有模型或工具执行器。"""

    product_store: AgentProductStore
    state_store: StateStore
    plan_store: PlanStore
    approval_gate: ApprovalGate
    job_store: JobStorageService
    files: FileStorageService | None
    stream_poll_seconds: float = 1.0


def create_agent_api_context(
    settings: AppSettings,
    *,
    files: FileStorageService,
    job_store: JobStorageService,
) -> AgentApiContext | None:
    if settings.storage_backend != "postgres" or not settings.runtime_database_url:
        return None
    database_url = settings.runtime_database_url
    return AgentApiContext(
        product_store=PostgresAgentProductStore(database_url),
        state_store=PostgresStateStore(database_url),
        plan_store=PostgresPlanStore(database_url),
        approval_gate=PostgresApprovalGate(database_url),
        job_store=job_store,
        files=files,
        stream_poll_seconds=max(0.1, settings.agent_poll_seconds),
    )
