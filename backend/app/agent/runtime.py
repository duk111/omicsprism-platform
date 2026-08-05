from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from time import monotonic
from typing import Any, Protocol
from uuid import uuid4

from ..models import JobStatus
from .approvals import ApprovalGate, ApprovalMismatch, InMemoryApprovalGate
from .audit import AgentEventStore, InMemoryAgentEventStore
from .context import MinimalContextBuilder
from .grounding import GroundedAnswerPipeline
from .model import ModelAdapter
from .plans import PlanStore, compute_plan_hash
from .policy import ProfilePolicyGuard
from .router import RuleRouter
from .schemas import (
    AgentAction,
    AgentApprovalBlock,
    AgentEvent,
    AgentEvidenceBlock,
    AgentJobBlock,
    AgentMessageBlock,
    AgentPlanBlock,
    AgentRecommendationBlock,
    AgentRecommendationItem,
    AgentState,
    AgentTextBlock,
    AgentTurnExecutionResult,
    AgentTurnRecord,
    ActiveProfile,
    ApprovalStatus,
    PlanRecord,
    RouteTargetProfile,
    RunState,
    RunStatus,
    ToolName,
    ToolResult,
)
from .store import StateNotFound, StateStore
from .tools import AgentToolRuntime, PolicyToolExecutor, ToolRegistry
from .validator import DecisionValidator


class RunCoordinator(Protocol):
    """单步运行时接口；本阶段不实现控制循环。"""

    def run_step(self, *, run_id: str, user_id: str, user_message: str) -> RunState:
        ...


class CoordinatorBudgetExceeded(RuntimeError):
    pass


class ProductionRunCoordinator:
    """生产 turn 的有界协调器；所有 I/O 都经注入的 store、model 与 tool 接口。"""

    def __init__(
        self,
        *,
        state_store: StateStore,
        plan_store: PlanStore,
        approval_gate: ApprovalGate,
        event_store: AgentEventStore,
        model: ModelAdapter,
        tool_runtime: AgentToolRuntime,
        max_transitions: int = 8,
        max_model_calls: int = 3,
        max_tool_calls: int = 6,
        timeout_seconds: float = 90.0,
    ) -> None:
        self.state_store = state_store
        self.plan_store = plan_store
        self.approvals = approval_gate
        self.events = event_store
        self.model = model
        self.tool_runtime = tool_runtime
        self.router = RuleRouter()
        self.context_builder = MinimalContextBuilder()
        self.validator = DecisionValidator()
        self.grounded_answers = GroundedAnswerPipeline()
        self.max_transitions = max_transitions
        self.max_model_calls = max_model_calls
        self.max_tool_calls = max_tool_calls
        self.timeout_seconds = timeout_seconds

    def execute_turn(self, *, turn: AgentTurnRecord, user_message: str,
                     persist: bool = True) -> AgentTurnExecutionResult:
        state = self.state_store.get(run_id=turn.run_id, user_id=turn.user_id)
        if state.thread_id != turn.thread_id or self.tool_runtime.user_id != turn.user_id:
            raise ApprovalMismatch("turn does not match the ownership-bound runtime")
        expected_version = state.version
        started = monotonic()
        blocks: list[AgentMessageBlock] = []
        transitions = 0
        turn_model_calls = 0
        turn_tool_calls = 0
        pending_events: list[AgentEvent] = []

        def call_model(context):
            nonlocal turn_model_calls
            if turn_model_calls >= self.max_model_calls:
                raise CoordinatorBudgetExceeded("agent model call budget exceeded")
            if monotonic() - started > self.timeout_seconds:
                raise CoordinatorBudgetExceeded("agent turn time budget exceeded")
            turn_model_calls += 1
            return self._model(context)

        def call_tool(executor: PolicyToolExecutor, name: ToolName, **kwargs: Any) -> ToolResult:
            nonlocal turn_tool_calls
            if turn_tool_calls >= self.max_tool_calls:
                raise CoordinatorBudgetExceeded("agent tool call budget exceeded")
            if monotonic() - started > self.timeout_seconds:
                raise CoordinatorBudgetExceeded("agent turn time budget exceeded")
            turn_tool_calls += 1
            return self._tool(executor, name, **kwargs)

        while transitions < self.max_transitions:
            if monotonic() - started > self.timeout_seconds:
                raise CoordinatorBudgetExceeded("agent turn time budget exceeded")
            transitions += 1

            if state.state in {AgentState.COLLECT_INTENT, AgentState.NEED_USER_INPUT, AgentState.AWAIT_FOLLOWUP}:
                route = self.router.route(user_message, state)
                pending_events.append(self._event(state, "route.decided", {
                    "intent": route.intent.value,
                    "target_profile": route.target_profile.value,
                }))
                if route.target_profile is RouteTargetProfile.ANALYSIS:
                    state.active_profile = ActiveProfile.ANALYSIS
                    state.state = AgentState.CHECK_INPUTS
                    continue
                if route.target_profile is RouteTargetProfile.INTERPRETATION:
                    state.active_profile = ActiveProfile.INTERPRETATION
                    state.state = AgentState.ANSWER_WITH_EVIDENCE
                    continue
                state.state = AgentState.NEED_USER_INPUT
                blocks.append(AgentTextBlock(text="请说明要运行分析，或指定一个已有结果进行解读。"))
                break

            if state.state is AgentState.CHECK_INPUTS:
                executor = self._executor(ActiveProfile.ANALYSIS)
                inspected = call_tool(executor, ToolName.INSPECT_UPLOADED_INPUTS)
                context = self.context_builder.build(
                    state=state,
                    active_profile=ActiveProfile.ANALYSIS,
                    user_message=user_message,
                    available_input_roles=[str(row.get("field")) for row in inspected.rows],
                )
                decision = call_model(context)
                self.validator.validate(state, decision)
                if decision.action is AgentAction.REQUEST_MORE_DATA:
                    state.state = AgentState.NEED_USER_INPUT
                    missing = decision.feasibility.missing_information if decision.feasibility else []
                    detail = "；".join(missing)
                    message = "请上传实际 CSV 文件并在发送前指定文件角色。"
                    if detail:
                        message += f"仍需补充：{detail}。"
                    message += "仅在消息中描述文件不等同于上传数据。"
                    blocks.append(AgentTextBlock(text=message))
                    break
                if not decision.analysis_recommendations:
                    raise ValueError("analysis decision has no recommendation")
                analysis_type = decision.analysis_recommendations[0]
                reasons = decision.feasibility.reasons if decision.feasibility else []
                blocks.append(AgentRecommendationBlock(recommendations=[AgentRecommendationItem(
                    analysis_type=analysis_type,
                    display_label=self.context_builder.analysis_specs.get(analysis_type).display_label,
                    reasons=reasons,
                )]))
                preflight = call_tool(
                    executor,
                    ToolName.RUN_PREFLIGHT,
                    analysis_type=analysis_type,
                    params=decision.requested_params,
                )
                if not preflight.ok or not preflight.rows:
                    state.state = AgentState.PREFLIGHT_BLOCKED
                    blocks.append(AgentTextBlock(text="输入预检未通过，请根据提示修正输入或参数。"))
                    break
                row = preflight.rows[0]
                plan = PlanRecord(
                    plan_id=f"plan-{uuid4()}",
                    run_id=state.run_id,
                    thread_id=state.thread_id,
                    user_id=state.user_id,
                    analysis_type=analysis_type,
                    input_source=self.tool_runtime.input_source_ref,
                    requested_params=dict(row.get("requested_params") or decision.requested_params),
                    effective_params=dict(row.get("effective_params") or {}),
                    contrasts=list(row.get("contrasts") or []),
                    plan_hash="pending",
                    approval_id=None,
                )
                plan.plan_hash = compute_plan_hash(plan)
                expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)
                self.plan_store.save(plan)
                approval_id = self.approvals.suspend(
                    plan_id=plan.plan_id,
                    thread_id=plan.thread_id,
                    run_id=plan.run_id,
                    user_id=plan.user_id,
                    plan_hash=plan.plan_hash,
                    expires_at=expires_at,
                )
                plan.approval_id = approval_id
                self.plan_store.save(plan)
                state.plan_id = plan.plan_id
                state.plan_hash = plan.plan_hash
                state.pending_approval_id = approval_id
                state.state = AgentState.WAIT_EXECUTION_CONFIRMATION
                state.status = RunStatus.SUSPENDED
                warnings = [_issue_text(item) for item in list(row.get("warnings") or [])]
                blocks.extend([
                    AgentPlanBlock(
                        plan_id=plan.plan_id,
                        plan_hash=plan.plan_hash,
                        analysis_type=plan.analysis_type,
                        requested_params=plan.requested_params,
                        effective_params=plan.effective_params,
                        contrasts=plan.contrasts,
                        warnings=warnings,
                        expires_at=expires_at,
                    ),
                    AgentApprovalBlock(
                        approval_id=approval_id,
                        plan_hash=plan.plan_hash,
                        status=ApprovalStatus.PENDING,
                        expires_at=expires_at,
                    ),
                ])
                break

            if state.state is AgentState.WAIT_EXECUTION_CONFIRMATION:
                if not state.pending_approval_id or not state.plan_hash or not self.approvals.is_valid(
                    approval_id=state.pending_approval_id,
                    run_id=state.run_id,
                    user_id=state.user_id,
                    plan_hash=state.plan_hash,
                ):
                    raise ApprovalMismatch("structured approval is required before submission")
                state.status = RunStatus.RUNNING
                state.state = AgentState.SUBMIT_JOBS
                continue

            if state.state is AgentState.SUBMIT_JOBS:
                if not state.plan_id:
                    raise RuntimeError("submission state has no plan")
                submitted = call_tool(
                    self._executor(ActiveProfile.ANALYSIS),
                    ToolName.SUBMIT_APPROVED_PLAN,
                    plan_id=state.plan_id,
                    idempotency_key=turn.idempotency_key,
                )
                if not submitted.ok or not submitted.rows:
                    raise RuntimeError(submitted.error_code or "job submission failed")
                job_ids = [str(item) for item in submitted.rows[0].get("job_ids", [])]
                state.focus.in_scope_job_ids = job_ids
                state.pending_approval_id = None
                state.state = AgentState.MONITOR_JOBS
                blocks.extend(_job_blocks(job_ids, status=JobStatus.QUEUED, progress=0))
                break

            if state.state is AgentState.MONITOR_JOBS:
                statuses = call_tool(
                    self._executor(state.active_profile),
                    ToolName.GET_JOBS_STATUS,
                    job_ids=state.focus.in_scope_job_ids,
                )
                if not statuses.ok:
                    raise RuntimeError(statuses.error_code or "job status failed")
                blocks.extend(_job_blocks_from_rows(statuses.rows))
                if statuses.rows and all(row.get("status") in {"succeeded", "failed", "cancelled"} for row in statuses.rows):
                    state.state = AgentState.AWAIT_FOLLOWUP
                break

            if state.state is AgentState.ANSWER_WITH_EVIDENCE:
                executor = self._executor(ActiveProfile.INTERPRETATION)
                query_context = self.context_builder.build(
                    state=state,
                    active_profile=ActiveProfile.INTERPRETATION,
                    user_message=user_message,
                )
                query_decision = call_model(query_context)
                self.validator.validate(state, query_decision)
                if query_decision.grounded_answer is not None:
                    raise ValueError("grounded answer is not allowed before evidence query")
                query = _safe_evidence_query(query_decision.requested_params, state.focus.in_scope_job_ids)
                evidence = call_tool(executor, ToolName.QUERY_RESULT_EVIDENCE, **query)
                if not evidence.ok:
                    raise RuntimeError(evidence.error_code or "evidence query failed")
                if evidence.rows:
                    answer_context = self.context_builder.build(
                        state=state,
                        active_profile=ActiveProfile.INTERPRETATION,
                        user_message=user_message,
                        evidence=evidence,
                    )
                    answer_decision = call_model(answer_context)
                    self.validator.validate(state, answer_decision)
                    if answer_decision.grounded_answer is None:
                        raise ValueError("grounded answer is required after evidence query")
                    answer = self.grounded_answers.answer(evidence, answer_decision.grounded_answer)
                else:
                    answer = self.grounded_answers.answer(evidence)
                self.grounded_answers.grounder.update_focus(state, answer)
                blocks.append(AgentEvidenceBlock(claims=answer.claims))
                state.state = AgentState.AWAIT_FOLLOWUP
                break

            if state.state in {AgentState.DONE, AgentState.JOB_FAILED, AgentState.PREFLIGHT_BLOCKED}:
                break

            raise RuntimeError(f"unsupported production state: {state.state.value}")

        if transitions >= self.max_transitions and not blocks:
            raise CoordinatorBudgetExceeded("agent transition budget exceeded")
        if turn_model_calls > self.max_model_calls or turn_tool_calls > self.max_tool_calls:
            raise CoordinatorBudgetExceeded("agent call budget exceeded")
        state.model_calls += turn_model_calls
        state.tool_calls += turn_tool_calls
        state.step_no += transitions
        completed_event = self._event(state, "turn.completed", {
            "turn_id": turn.turn_id,
            "state": state.state.value,
            "model_calls": turn_model_calls,
            "tool_calls": turn_tool_calls,
        })
        pending_events.append(completed_event)
        if persist:
            self.state_store.save(state, expected_version=expected_version)
            saved = self.state_store.get(run_id=state.run_id, user_id=state.user_id)
            for event in pending_events:
                self.events.append(event.model_copy(update={"step_no": saved.step_no}))
        else:
            saved = state.model_copy(update={"version": expected_version + 1})
        return AgentTurnExecutionResult(
            state=saved,
            blocks=blocks,
            expected_version=expected_version,
            events=pending_events,
        )

    def _executor(self, profile: ActiveProfile) -> PolicyToolExecutor:
        return PolicyToolExecutor(
            ToolRegistry(self.tool_runtime),
            runtime=self.tool_runtime,
            active_profile=profile,
            policy=ProfilePolicyGuard(),
        )

    def _model(self, context):
        return self.model.decide(context)

    @staticmethod
    def _tool(executor: PolicyToolExecutor, name: ToolName, **kwargs: Any) -> ToolResult:
        return executor.execute(name, **kwargs)

    @staticmethod
    def _event(state: RunState, event_type: str, payload: dict[str, Any]) -> AgentEvent:
        return AgentEvent(
            event_id=str(uuid4()),
            run_id=state.run_id,
            user_id=state.user_id,
            step_no=state.step_no,
            event_type=event_type,
            payload=payload,
        )


class FixtureRunCoordinator:
    """Phase 2 的单步 fixture harness；不调用真实工具，也不创建业务 job。"""

    def __init__(self, *, state_store: StateStore, model: ModelAdapter,
                 approval_gate: InMemoryApprovalGate, event_store: AgentEventStore | None = None) -> None:
        self.state_store = state_store
        self.model = model
        self.router = RuleRouter()
        self.context_builder = MinimalContextBuilder()
        self.validator = DecisionValidator()
        self.approvals = approval_gate
        self.events = event_store or InMemoryAgentEventStore()
        self.created_job_ids: list[str] = []

    @classmethod
    def create(cls, *, state_store: StateStore, model: ModelAdapter, initial_state: RunState,
               approval_gate: InMemoryApprovalGate | None = None,
               event_store: AgentEventStore | None = None) -> "FixtureRunCoordinator":
        try:
            state_store.get(run_id=initial_state.run_id, user_id=initial_state.user_id)
        except StateNotFound:
            state_store.save(initial_state, expected_version=0)
        return cls(
            state_store=state_store,
            model=model,
            approval_gate=approval_gate or InMemoryApprovalGate(),
            event_store=event_store,
        )

    def run_step(self, *, run_id: str, user_id: str, user_message: str) -> RunState:
        state = self.state_store.get(run_id=run_id, user_id=user_id)
        expected = state.version

        if state.status is RunStatus.SUSPENDED:
            if not _is_explicit_approval(user_message):
                raise ApprovalMismatch("suspended run requires an explicit approval confirmation")
            if not state.pending_approval_id or not state.plan_hash:
                raise RuntimeError("suspended run has no approval")
            self.approvals.resume(
                approval_id=state.pending_approval_id, run_id=run_id, user_id=user_id,
                plan_hash=state.plan_hash,
            )
            state.status = RunStatus.RUNNING
            state.pending_approval_id = None
            state.active_profile = ActiveProfile.INTERPRETATION
            state.focus.in_scope_job_ids = ["fixture-job-1"]
            state.state = AgentState.ANSWER_WITH_EVIDENCE
        elif state.state is AgentState.COLLECT_INTENT:
            route = self.router.route(user_message, state)
            self._record_route(state, route)
            if route.target_profile is RouteTargetProfile.ANALYSIS:
                state.active_profile = ActiveProfile.ANALYSIS
                state.state = AgentState.CHECK_INPUTS
            elif route.target_profile is RouteTargetProfile.INTERPRETATION:
                state.active_profile = ActiveProfile.INTERPRETATION
                state.state = AgentState.ANSWER_WITH_EVIDENCE
            else:
                state.state = AgentState.NEED_USER_INPUT
        elif state.state is AgentState.NEED_USER_INPUT:
            route = self.router.route(user_message, state)
            self._record_route(state, route)
            if route.target_profile is RouteTargetProfile.ANALYSIS:
                state.active_profile = ActiveProfile.ANALYSIS
                state.state = AgentState.CHECK_INPUTS
            elif route.target_profile is RouteTargetProfile.INTERPRETATION:
                state.active_profile = ActiveProfile.INTERPRETATION
                state.state = AgentState.ANSWER_WITH_EVIDENCE
        elif state.state is AgentState.AWAIT_FOLLOWUP:
            route = self.router.route(user_message, state)
            self._record_route(state, route)
            if route.target_profile is RouteTargetProfile.ANALYSIS:
                state.active_profile = ActiveProfile.ANALYSIS
                state.state = AgentState.CHECK_INPUTS
            elif route.target_profile is RouteTargetProfile.INTERPRETATION:
                state.active_profile = ActiveProfile.INTERPRETATION
                state.state = AgentState.ANSWER_WITH_EVIDENCE
            else:
                state.state = AgentState.NEED_USER_INPUT
        elif state.state is AgentState.ANSWER_WITH_EVIDENCE:
            route = self.router.route(user_message, state)
            if route.target_profile is RouteTargetProfile.ANALYSIS:
                self._record_route(state, route)
                state.active_profile = ActiveProfile.ANALYSIS
                state.state = AgentState.CHECK_INPUTS
            else:
                context = self.context_builder.build(state=state, active_profile=state.active_profile, user_message=user_message)
                decision = self.model.decide(context)
                state.model_calls += 1
                self.validator.validate(state, decision)
                if decision.action is AgentAction.ANSWER:
                    state.state = AgentState.AWAIT_FOLLOWUP
        else:
            context = self.context_builder.build(state=state, active_profile=state.active_profile, user_message=user_message)
            decision = self.model.decide(context)
            state.model_calls += 1
            self.validator.validate(state, decision)
            if state.state is AgentState.CHECK_INPUTS and decision.action is AgentAction.PROPOSE_PLAN:
                state.plan_id = "fixture-plan-1"
                canonical_plan = json.dumps(decision.requested_params, sort_keys=True, separators=(",", ":"))
                state.plan_hash = "sha256:" + sha256(canonical_plan.encode()).hexdigest()
                state.state = AgentState.WAIT_PLAN_CONFIRMATION
            elif state.state is AgentState.WAIT_PLAN_CONFIRMATION and decision.action is AgentAction.REQUEST_APPROVAL:
                state.pending_approval_id = self.approvals.suspend(
                    run_id=run_id, user_id=user_id, plan_hash=state.plan_hash or "",
                    expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
                )
                state.state = AgentState.WAIT_EXECUTION_CONFIRMATION
                state.status = RunStatus.SUSPENDED
        state.step_no += 1
        self.state_store.save(state, expected_version=expected)
        saved = self.state_store.get(run_id=run_id, user_id=user_id)
        self._record_state(saved)
        return saved

    def _record_route(self, state: RunState, route) -> None:
        self.events.append(AgentEvent(
            event_id=str(uuid4()), run_id=state.run_id, user_id=state.user_id,
            step_no=state.step_no, event_type="route.decided",
            payload={"intent": route.intent.value, "target_profile": route.target_profile.value},
        ))

    def _record_state(self, state: RunState) -> None:
        self.events.append(AgentEvent(
            event_id=str(uuid4()), run_id=state.run_id, user_id=state.user_id,
            step_no=state.step_no, event_type="state.updated",
            payload={"state": state.state.value, "active_profile": state.active_profile.value},
        ))


def _is_explicit_approval(user_message: str) -> bool:
    text = user_message.strip().lower()
    return any(term in text for term in ("批准", "同意执行", "approve", "confirm execution"))


def _issue_text(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("message") or item.get("code") or "preflight warning")
    return str(item)


def _job_blocks(job_ids: list[str], *, status: JobStatus, progress: int) -> list[AgentJobBlock]:
    return [
        AgentJobBlock(
            job_id=job_id,
            status=status,
            progress=progress,
            progress_url=f"/api/jobs/{job_id}/progress",
            results_url=f"/results/{job_id}" if status is JobStatus.SUCCEEDED else None,
        )
        for job_id in job_ids
    ]


def _job_blocks_from_rows(rows: list[dict[str, Any]]) -> list[AgentJobBlock]:
    blocks = []
    for row in rows:
        status = JobStatus(str(row.get("status")))
        job_id = str(row.get("job_id"))
        blocks.extend(_job_blocks(
            [job_id],
            status=status,
            progress=max(0, min(100, int(row.get("progress") or 0))),
        ))
    return blocks


def _safe_evidence_query(params: dict[str, Any], focus_job_ids: list[str]) -> dict[str, Any]:
    job_id = str(params.get("job_id") or (focus_job_ids[0] if focus_job_ids else ""))
    if not job_id or job_id not in focus_job_ids:
        raise ValueError("evidence query job is outside the run focus")
    artifact = params.get("artifact")
    if not isinstance(artifact, str) or not artifact.strip():
        raise ValueError("evidence query requires an artifact")
    query: dict[str, Any] = {"job_id": job_id, "artifact": artifact.strip()}
    for name in ("sort", "resolve_entity"):
        value = params.get(name)
        if value is not None:
            if not isinstance(value, str):
                raise ValueError(f"evidence query {name} must be text")
            query[name] = value
    limit = params.get("limit")
    if limit is not None:
        if not isinstance(limit, int) or isinstance(limit, bool):
            raise ValueError("evidence query limit must be an integer")
        query["limit"] = max(1, min(50, limit))
    return query
