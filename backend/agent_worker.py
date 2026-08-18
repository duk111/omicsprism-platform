from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime, timezone
import logging
import os
import socket
import time
from typing import Protocol

import httpx

from .app.agent.approvals import ApprovalExpired, ApprovalMismatch, PostgresApprovalGate
from .app.agent.audit import PostgresAgentEventStore
from .app.agent.model import (
    ModelBoundaryError,
    ModelUnavailableError,
    VllmModelAdapter,
)
from .app.agent.plans import PostgresPlanStore
from .app.agent.product_store import AgentProductStore, PostgresAgentProductStore
from .app.agent.runtime import CoordinatorBudgetExceeded, ProductionRunCoordinator
from .app.agent.context import build_conversation_summary
from .app.agent.schemas import (
    AgentErrorBlock,
    AgentInputSourceKind,
    AgentMessageBlock,
    AgentMessageRecord,
    AgentMessageRole,
    AgentTurnExecutionResult,
    AgentTurnRecord,
    AgentTurnStatus,
    ActiveProfile,
)
from .app.agent.store import PostgresStateStore, StateConflict
from .app.agent.tools import (
    AgentToolRuntime,
    ExistingJobInputSource,
    StagedBundleInputSource,
)
from .app.agent.validator import InvalidDecision
from .app.job_execution import RedisJobExecutor, RedisJobQueue
from .app.job_store import JobStorageService, PostgresJobRepository
from .app.settings import AppSettings, load_settings
from .app.storage_service import FileStorageService


LOGGER = logging.getLogger("omicsprism.agent_worker")


class TurnProcessor(Protocol):
    def process(self, turn: AgentTurnRecord) -> AgentTurnExecutionResult | Sequence[AgentMessageBlock]:
        ...


class AgentWorker:
    """全局串行 turn worker；数据库 lease 负责 crash recovery。"""

    def __init__(self, *, store: AgentProductStore, processor: TurnProcessor,
                 worker_id: str, lease_seconds: int = 120, max_attempts: int = 3,
                 clock: Callable[[], datetime] | None = None) -> None:
        if not worker_id or lease_seconds < 1 or max_attempts < 1:
            raise ValueError("worker id, lease and max attempts must be positive")
        self.store = store
        self.processor = processor
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.max_attempts = max_attempts
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def run_once(self) -> bool:
        with self.store.worker_slot() as acquired:
            if not acquired:
                return False
            now = self.clock()
            turn = self.store.claim_next_turn(
                worker_id=self.worker_id,
                now=now,
                lease_seconds=self.lease_seconds,
            )
            if turn is None:
                return False
            if turn.attempt > self.max_attempts:
                self._fail(turn, "max_attempts_exceeded", "该请求多次恢复失败，请重新提交。", False)
                return True
            try:
                processed = self.processor.process(turn)
            except Exception as exc:
                code, user_message, retryable = _classify_error(exc)
                LOGGER.warning(
                    "agent turn failed: code=%s error_type=%s detail=%s",
                    code,
                    type(exc).__name__,
                    str(exc),
                    extra={"turn_id": turn.turn_id, "error_code": code},
                )
                self._fail(turn, code, user_message, retryable)
                return True
            if isinstance(processed, AgentTurnExecutionResult):
                commit = getattr(self.store, "commit_turn_result", None)
                if commit is None:
                    self._fail(
                        turn,
                        "atomic_checkpoint_unavailable",
                        "当前存储不支持安全提交，请联系管理员。",
                        False,
                    )
                    return True
                commit(
                    turn=turn,
                    worker_id=self.worker_id,
                    state=processed.state,
                    expected_version=processed.expected_version,
                    blocks=processed.blocks,
                    events=processed.events,
                    now=self.clock(),
                )
                return True
            blocks = list(processed)
            if blocks:
                self._append_once(turn, blocks)
            self.store.finish_turn(
                turn_id=turn.turn_id,
                user_id=turn.user_id,
                worker_id=self.worker_id,
                status=AgentTurnStatus.COMPLETED,
                now=self.clock(),
            )
            return True

    def _fail(self, turn: AgentTurnRecord, code: str, user_message: str, retryable: bool) -> None:
        self._append_once(turn, [AgentErrorBlock(
            code=code,
            user_message=user_message,
            retryable=retryable,
            request_id=None,
        )])
        self.store.finish_turn(
            turn_id=turn.turn_id,
            user_id=turn.user_id,
            worker_id=self.worker_id,
            status=AgentTurnStatus.FAILED,
            now=self.clock(),
            error_code=code,
        )

    def _append_once(self, turn: AgentTurnRecord, blocks: list[AgentMessageBlock]) -> None:
        message_id = f"assistant-{turn.turn_id}"
        existing = self.store.list_messages(thread_id=turn.thread_id, user_id=turn.user_id)
        if any(item.message_id == message_id for item in existing):
            return
        self.store.append_message(AgentMessageRecord(
            message_id=message_id,
            thread_id=turn.thread_id,
            run_id=turn.run_id,
            user_id=turn.user_id,
            role=AgentMessageRole.ASSISTANT,
            blocks=blocks,
            created_at=self.clock(),
        ))


class ProductionTurnProcessor:
    """按 turn 装配真实 repository、输入源、模型与工具运行时。"""

    def __init__(self, *, product_store: AgentProductStore, state_store: PostgresStateStore,
                 plan_store: PostgresPlanStore, approvals: PostgresApprovalGate,
                 events: PostgresAgentEventStore, model: VllmModelAdapter,
                 job_store: JobStorageService, files: FileStorageService,
                 executor: RedisJobExecutor, timeout_seconds: float = 90.0,
                 max_model_calls: int = 6) -> None:
        self.product_store = product_store
        self.state_store = state_store
        self.plan_store = plan_store
        self.approvals = approvals
        self.events = events
        self.model = model
        self.job_store = job_store
        self.files = files
        self.executor = executor
        self.timeout_seconds = timeout_seconds
        self.max_model_calls = max_model_calls

    def process(self, turn: AgentTurnRecord) -> AgentTurnExecutionResult:
        state = self.state_store.get(run_id=turn.run_id, user_id=turn.user_id)
        messages = self.product_store.list_messages(thread_id=turn.thread_id, user_id=turn.user_id)
        user_message = _latest_user_text(messages)
        conversation_summary = build_conversation_summary(messages[:-1] if messages and messages[-1].role is AgentMessageRole.USER else messages)
        source_ref = _select_input_source(
            state=state,
            messages=messages,
            plan_store=self.plan_store,
            user_id=turn.user_id,
        )

        if source_ref is None:
            tool_runtime = AgentToolRuntime(
                user_id=turn.user_id,
                inputs={},
                plans=self.plan_store,
                job_store=self.job_store,
                files=self.files,
                executor=self.executor,
                approval_gate=self.approvals,
            )
        else:
            if source_ref.kind is AgentInputSourceKind.EXISTING_JOB:
                source = ExistingJobInputSource(
                    user_id=turn.user_id,
                    source_job_id=source_ref.source_id,
                    job_store=self.job_store,
                    files=self.files,
                )
            else:
                source = StagedBundleInputSource(
                    user_id=turn.user_id,
                    thread_id=turn.thread_id,
                    bundle_id=source_ref.source_id,
                    product_store=self.product_store,
                    files=self.files,
                )
            tool_runtime = AgentToolRuntime.from_input_source(
                user_id=turn.user_id,
                input_source=source,
                plans=self.plan_store,
                job_store=self.job_store,
                files=self.files,
                executor=self.executor,
                approval_gate=self.approvals,
            )

        return ProductionRunCoordinator(
            state_store=self.state_store,
            plan_store=self.plan_store,
            approval_gate=self.approvals,
            event_store=self.events,
            model=self.model,
            tool_runtime=tool_runtime,
            timeout_seconds=self.timeout_seconds,
            max_model_calls=self.max_model_calls,
        ).execute_turn(
            turn=turn,
            user_message=user_message,
            conversation_summary=conversation_summary,
            persist=False,
        )


def create_worker(settings: AppSettings | None = None) -> AgentWorker:
    config = settings or load_settings()
    if not config.runtime_database_url:
        raise RuntimeError("OMICS_PRISM_RUNTIME_DATABASE_URL is required for agent worker")
    if config.storage_backend != "postgres":
        raise RuntimeError("agent worker requires OMICS_PRISM_STORAGE_BACKEND=postgres")
    if config.executor_backend != "redis":
        raise RuntimeError("agent worker requires OMICS_PRISM_EXECUTOR=redis")
    if not config.agent_model_url or not config.agent_model_name:
        raise RuntimeError("OMICS_PRISM_AGENT_MODEL_URL and OMICS_PRISM_AGENT_MODEL_NAME are required")
    if config.agent_model_request_timeout_seconds * 2 > config.agent_turn_timeout_seconds:
        # 单次模型请求超时 × 2（含一次 schema 修复的第二次请求）应当小于 turn 预算，
        # 否则慢模型的一次修复就能耗尽整个 turn。只告警不 raise，避免配置漂移起不来。
        LOGGER.warning(
            "agent model request timeout (%.0fs) x2 exceeds the turn budget (%.0fs); "
            "raise OMICS_PRISM_AGENT_TURN_TIMEOUT_SECONDS or lower "
            "OMICS_PRISM_AGENT_MODEL_REQUEST_TIMEOUT_SECONDS.",
            config.agent_model_request_timeout_seconds,
            config.agent_turn_timeout_seconds,
        )
    if config.agent_max_model_calls < 6:
        # 计费口径是真实 HTTP 次数：解读 turn 最多 3 次 decide（查询 + 重试 + 答案），
        # 每次 decide 在 schema 修复时会发 2 次 HTTP。低于 6 时证据重试链路必然被截断。
        LOGGER.warning(
            "agent max model calls (%d) is below the 6 HTTP requests a full interpretation "
            "turn can need; evidence retry or schema repair will be cut short.",
            config.agent_max_model_calls,
        )

    files = FileStorageService(config)
    jobs = JobStorageService(PostgresJobRepository(config.runtime_database_url))
    files.attach_job_store(jobs)
    queue = RedisJobQueue(config.redis_url, config.redis_queue_name)
    executor = RedisJobExecutor(queue)
    product_store = PostgresAgentProductStore(config.runtime_database_url)
    processor = ProductionTurnProcessor(
        product_store=product_store,
        state_store=PostgresStateStore(config.runtime_database_url),
        plan_store=PostgresPlanStore(config.runtime_database_url),
        approvals=PostgresApprovalGate(config.runtime_database_url),
        events=PostgresAgentEventStore(config.runtime_database_url),
        model=VllmModelAdapter(
            base_url=config.agent_model_url,
            model=config.agent_model_name,
            api_key=config.agent_model_api_key,
            timeout_seconds=config.agent_model_request_timeout_seconds,
        ),
        job_store=jobs,
        files=files,
        executor=executor,
        timeout_seconds=config.agent_turn_timeout_seconds,
        max_model_calls=config.agent_max_model_calls,
    )
    worker_id = os.getenv("OMICS_PRISM_AGENT_WORKER_ID") or f"{socket.gethostname()}-{os.getpid()}"
    return AgentWorker(
        store=product_store,
        processor=processor,
        worker_id=worker_id,
        lease_seconds=config.agent_lease_seconds,
        max_attempts=config.agent_max_attempts,
    )


def main() -> None:
    settings = load_settings()
    logging.basicConfig(level=settings.log_level)
    worker = create_worker(settings)
    while True:
        try:
            worked = worker.run_once()
        except KeyboardInterrupt:
            return
        except Exception:
            LOGGER.exception("agent worker loop failed")
            worked = False
        if not worked:
            time.sleep(settings.agent_poll_seconds)


def _latest_user_text(messages) -> str:
    for message in reversed(messages):
        if message.role is not AgentMessageRole.USER:
            continue
        for block in reversed(message.blocks):
            if block.type == "text":
                return block.text
    return ""


def _latest_input_source(messages, focus_job_ids):
    for message in reversed(messages):
        for block in reversed(message.blocks):
            if block.type == "input_summary":
                from .app.agent.schemas import AgentInputSourceRef
                return AgentInputSourceRef(kind=AgentInputSourceKind.STAGED_BUNDLE, source_id=block.bundle_id)
    if focus_job_ids:
        from .app.agent.schemas import AgentInputSourceRef
        return AgentInputSourceRef(kind=AgentInputSourceKind.EXISTING_JOB, source_id=focus_job_ids[0])
    return None


def _select_input_source(*, state, messages, plan_store, user_id):
    """待审批计划锁定原输入；其他对话优先采用用户最新显式上传。"""
    if state.plan_id and state.pending_approval_id:
        return plan_store.get(plan_id=state.plan_id, user_id=user_id).input_source
    latest = _latest_input_source(messages, state.focus.in_scope_job_ids)
    if latest is not None:
        return latest
    if state.plan_id:
        return plan_store.get(plan_id=state.plan_id, user_id=user_id).input_source
    return None


def _classify_error(exc: Exception) -> tuple[str, str, bool]:
    if isinstance(exc, ModelUnavailableError):
        return "model_unavailable", "Copilot 模型暂时不可用，请稍后重试。", True
    if isinstance(exc, httpx.TimeoutException):
        return "model_timeout", "Copilot 模型响应超时，请稍后重试。", True
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        if status_code in {408, 429} or status_code >= 500:
            return "model_unavailable", "Copilot 模型暂时不可用，请稍后重试。", True
        return (
            "model_request_rejected",
            "Copilot 模型未能接受本次结果解读上下文。当前任务和结果均已保留；请重试，若持续失败请联系管理员。",
            True,
        )
    if isinstance(exc, httpx.RequestError):
        return "model_unavailable", "Copilot 模型暂时不可用，请稍后重试。", True
    if isinstance(exc, ModelBoundaryError):
        return (
            "invalid_model_response",
            "模型两次都未能生成当前步骤需要的结构化结果。当前会话上下文、上传文件和已有任务均保留，"
            "且未创建任务；请明确要分析的数据或要解读的结果后重试。",
            True,
        )
    if isinstance(exc, InvalidDecision):
        return (
            "model_decision_conflict",
            "模型回复与当前会话状态不一致，因此系统没有执行写操作，也未创建任务。"
            "会话上下文、上传文件和已有任务均保留；请重试或明确当前要分析还是解读结果。",
            True,
        )
    if isinstance(exc, ApprovalExpired):
        return (
            "approval_expired",
            "该计划的审批已过期，未创建任务。请重新生成计划后再执行。",
            False,
        )
    if isinstance(exc, ApprovalMismatch):
        return (
            "approval_required",
            "当前操作需要先完成计划审批；请在计划卡片上批准或拒绝后再继续。",
            False,
        )
    if isinstance(exc, ValueError):
        return "agent_decision_invalid", "当前请求缺少可安全执行的信息，且未创建任务。请补充分析目标或参数。", True
    if isinstance(exc, StateConflict):
        return "state_conflict", "对话状态已更新，请刷新后重试。", True
    if isinstance(exc, CoordinatorBudgetExceeded):
        return "turn_budget_exceeded", "本次处理超过安全预算，请缩小问题范围后重试。", True
    return "agent_turn_failed", "Copilot 处理失败，请稍后重试。", True


if __name__ == "__main__":
    main()
