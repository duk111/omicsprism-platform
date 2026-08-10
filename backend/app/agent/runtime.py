from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import re
from time import monotonic
from typing import Any, Protocol
from uuid import uuid4

from ..models import AnalysisType, JobStatus
from .approvals import ApprovalGate, ApprovalMismatch, InMemoryApprovalGate
from .audit import AgentEventStore, InMemoryAgentEventStore
from .context import MinimalContextBuilder, build_input_summaries
from .grounding import GroundedAnswerPipeline
from .model import ModelAdapter
from .plans import PlanNotFound, PlanStore, compute_plan_hash
from .policy import ProfilePolicyGuard
from .router import RuleRouter
from .schemas import (
    AgentAction,
    AgentAdvisoryBlock,
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
    AdvisoryCategory,
    ApprovalStatus,
    InputInspectionSummary,
    PlanRecord,
    RouteIntent,
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
        advisory_category = AdvisoryCategory.GENERAL_BIOLOGY
        advisory_input_roles: list[str] = []
        advisory_input_summaries = []
        advisory_needs_inputs = False

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

            # 终止/阻塞状态仍允许用户在同一会话继续提问或修正输入。
            # 只有等待审批的状态不能被普通消息绕过；审批继续走专门的 resume turn。
            if state.state in {
                AgentState.COLLECT_INTENT,
                AgentState.NEED_USER_INPUT,
                AgentState.AWAIT_FOLLOWUP,
                AgentState.DONE,
                AgentState.JOB_FAILED,
                AgentState.PREFLIGHT_BLOCKED,
            }:
                route = self.router.route(user_message, state)
                pending_events.append(self._event(state, "route.decided", {
                    "intent": route.intent.value,
                    "target_profile": route.target_profile.value,
                }))
                if route.target_profile is RouteTargetProfile.ANALYSIS:
                    state.active_profile = ActiveProfile.ANALYSIS
                    if route.intent is RouteIntent.EXPLAIN_PLAN:
                        blocks.append(AgentAdvisoryBlock(
                            category=AdvisoryCategory.ANALYSIS_GUIDANCE,
                            text=self._explain_current_plan(state),
                        ))
                        state.state = AgentState.AWAIT_FOLLOWUP
                        break
                    if route.intent is RouteIntent.HELP:
                        blocks.append(AgentAdvisoryBlock(
                            category=AdvisoryCategory.ANALYSIS_GUIDANCE,
                            text=_CAPABILITY_HELP,
                        ))
                        state.state = AgentState.AWAIT_FOLLOWUP
                        break
                    if (
                        route.intent in {RouteIntent.DESCRIBE_ONLY, RouteIntent.UNCLEAR}
                        and self.tool_runtime.inputs
                    ):
                        inspected = call_tool(
                            self._executor(ActiveProfile.ANALYSIS),
                            ToolName.INSPECT_UPLOADED_INPUTS,
                        )
                        roles = [str(row.get("field")) for row in inspected.rows]
                        blocks.append(AgentAdvisoryBlock(
                            category=AdvisoryCategory.ANALYSIS_GUIDANCE,
                            text=_input_receipt_text(roles, self.context_builder),
                        ))
                        state.state = AgentState.AWAIT_FOLLOWUP
                        break
                    if route.intent in {RouteIntent.DESCRIBE_ONLY, RouteIntent.UNCLEAR}:
                        advisory_category = (
                            AdvisoryCategory.ANALYSIS_GUIDANCE
                            if route.intent is RouteIntent.DESCRIBE_ONLY
                            else AdvisoryCategory.GENERAL_BIOLOGY
                        )
                        state.state = AgentState.ADVISE
                    else:
                        state.state = AgentState.CHECK_INPUTS
                    continue
                if route.target_profile is RouteTargetProfile.INTERPRETATION:
                    state.active_profile = ActiveProfile.INTERPRETATION
                    state.state = AgentState.ANSWER_WITH_EVIDENCE
                    continue
                state.state = AgentState.NEED_USER_INPUT
                blocks.append(AgentTextBlock(text="请说明要运行分析，或指定一个已有结果进行解读。"))
                break

            if state.state is AgentState.ADVISE:
                context = self.context_builder.build(
                    state=state,
                    active_profile=ActiveProfile.ANALYSIS,
                    user_message=user_message,
                    available_input_roles=advisory_input_roles,
                    input_summaries=advisory_input_summaries,
                )
                decision = call_model(context)
                self.validator.validate(state, decision)
                blocks.append(AgentAdvisoryBlock(
                    category=advisory_category,
                    text=decision.advisory_answer or "当前无法生成咨询回答。",
                ))
                state.state = AgentState.NEED_USER_INPUT if advisory_needs_inputs else AgentState.AWAIT_FOLLOWUP
                break

            if state.state is AgentState.CHECK_INPUTS:
                executor = self._executor(ActiveProfile.ANALYSIS)
                inspected = call_tool(executor, ToolName.INSPECT_UPLOADED_INPUTS)
                available_input_roles = [str(row.get("field")) for row in inspected.rows]
                input_summaries = build_input_summaries(inspected.rows)
                role_set = set(available_input_roles)
                has_supported_inputs = any(
                    all(rule.name in role_set for rule in spec.input_rules if rule.required)
                    for analysis_type in self.context_builder.analysis_specs.analysis_types()
                    for spec in [self.context_builder.analysis_specs.get(analysis_type)]
                )
                if not has_supported_inputs:
                    if available_input_roles:
                        blocks.append(AgentAdvisoryBlock(
                            category=AdvisoryCategory.ANALYSIS_GUIDANCE,
                            text=_input_receipt_text(available_input_roles, self.context_builder),
                        ))
                        state.state = AgentState.NEED_USER_INPUT
                        break
                    advisory_category = AdvisoryCategory.ANALYSIS_GUIDANCE
                    advisory_input_roles = available_input_roles
                    advisory_input_summaries = input_summaries
                    advisory_needs_inputs = True
                    state.state = AgentState.ADVISE
                    continue
                context = self.context_builder.build(
                    state=state,
                    active_profile=ActiveProfile.ANALYSIS,
                    user_message=user_message,
                    available_input_roles=available_input_roles,
                    input_summaries=input_summaries,
                )
                decision = call_model(context)
                self.validator.validate(state, decision)
                if decision.action is AgentAction.REQUEST_MORE_DATA:
                    state.state = AgentState.NEED_USER_INPUT
                    missing = decision.feasibility.missing_information if decision.feasibility else []
                    detail = "；".join(missing)
                    message = "已识别上传文件，但生成分析计划前还需要补充信息。"
                    if detail:
                        message += f"请补充：{detail}"
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
                requested_params, contrast_question = _complete_contrast_params(
                    analysis_type,
                    decision.requested_params,
                    input_summaries,
                )
                if contrast_question:
                    state.state = AgentState.NEED_USER_INPUT
                    blocks.append(AgentTextBlock(text=contrast_question))
                    break
                preflight = call_tool(
                    executor,
                    ToolName.RUN_PREFLIGHT,
                    analysis_type=analysis_type,
                    params=requested_params,
                )
                if not preflight.ok or not preflight.rows:
                    state.state = AgentState.PREFLIGHT_BLOCKED
                    errors = (
                        [_issue_text(item) for item in list(preflight.rows[0].get("errors") or [])]
                        if preflight.rows else []
                    )
                    detail = "；".join(errors[:3])
                    message = "输入预检未通过。"
                    if detail:
                        message += f"{detail}"
                    else:
                        message += "请检查文件角色、样本名、分组列和比较参数。"
                    blocks.append(AgentTextBlock(text=message))
                    break
                row = preflight.rows[0]
                plan = PlanRecord(
                    plan_id=f"plan-{uuid4()}",
                    run_id=state.run_id,
                    thread_id=state.thread_id,
                    user_id=state.user_id,
                    analysis_type=analysis_type,
                    input_source=self.tool_runtime.input_source_ref,
                    requested_params=dict(row.get("requested_params") or requested_params),
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

    def _explain_current_plan(self, state: RunState) -> str:
        if not state.plan_id:
            return "当前会话没有可解释的分析计划。请先上传数据并说明分析目标。"
        try:
            plan = self.plan_store.get(plan_id=state.plan_id, user_id=state.user_id)
        except PlanNotFound:
            return "当前分析计划已不可用，请根据现有输入重新生成计划。"
        label = {
            AnalysisType.DIFFERENTIAL: "DEG 差异表达",
            AnalysisType.DEM: "DEM 差异代谢",
            AnalysisType.CORRELATION: "GMA 转录组-代谢组关联",
        }[plan.analysis_type]
        details = []
        for contrast in plan.contrasts[:5]:
            field = str(contrast.get("compare_field") or "分组列")
            tested = str(contrast.get("tested_level") or "实验组")
            reference = str(contrast.get("reference_level") or "对照组")
            tested_count = int(contrast.get("tested_count") or 0)
            reference_count = int(contrast.get("reference_count") or 0)
            details.append(
                f"按 {field} 比较 {tested}（{tested_count} 个样本）与 "
                f"{reference}（{reference_count} 个样本）"
            )
        comparison = "；".join(details) or "使用当前上传的三类组学输入进行关联分析"
        params = plan.effective_params
        thresholds = []
        if "padj_cutoff" in params:
            thresholds.append(f"校正后 P 值阈值 {params['padj_cutoff']}")
        if "log2fc_cutoff" in params:
            thresholds.append(f"|log2FC| 阈值 {params['log2fc_cutoff']}")
        if "min_total_count" in params:
            thresholds.append(f"最低总计数 {params['min_total_count']}")
        threshold_text = "，".join(thresholds)
        explanation = f"这是一个 {label} 计划：{comparison}。"
        if threshold_text:
            explanation += f"筛选设置为{threshold_text}。"
        explanation += "计划本身不会创建任务；只有批准与该计划哈希绑定的审批后才会提交。"
        if state.pending_approval_id is None:
            explanation += "当前计划没有有效的待审批请求，不会执行；你可以修改比较条件后重新生成。"
        return explanation

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


def _input_receipt_text(roles: list[str], context_builder: MinimalContextBuilder) -> str:
    role_set = set(roles)
    supported = [
        context_builder.analysis_specs.get(analysis_type).display_label
        for analysis_type in context_builder.analysis_specs.analysis_types()
        for spec in [context_builder.analysis_specs.get(analysis_type)]
        if all(rule.name in role_set for rule in spec.input_rules if rule.required)
    ]
    role_text = "、".join(sorted(role_set)) or "无"
    if supported:
        return (
            f"已收到文件角色：{role_text}。当前组合可用于 {'、'.join(supported)}。"
            "如需生成分析计划，请说明分析目标；差异分析还需要确认比较列、实验组和对照组。"
        )
    return (
        f"已收到文件角色：{role_text}，但尚未形成完整分析输入组合。"
        "DEG 需要 counts + metadata，DEM 需要 metabs + metadata，"
        "GMA 需要 transcriptome + metabolome + group。"
    )


def _issue_text(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("message") or item.get("code") or "preflight warning")
    return str(item)


_REFERENCE_LEVEL_MARKERS = {
    "control", "ctrl", "ck", "wt", "mock", "untreated",
    "对照", "空白", "野生型", "未处理",
}


def _complete_contrast_params(
    analysis_type: AnalysisType,
    requested: dict[str, Any],
    summaries: list[InputInspectionSummary],
) -> tuple[dict[str, Any], str | None]:
    """仅从真实 metadata 二水平分组补齐安全 contrast；歧义时请求用户确认。"""

    params = dict(requested)
    if analysis_type not in {AnalysisType.DIFFERENTIAL, AnalysisType.DEM}:
        return params, None
    required = ("compare_field", "tested_levels", "reference_level")
    if all(str(params.get(name) or "").strip() for name in required):
        return params, None

    metadata = next((item for item in summaries if item.field == "metadata"), None)
    groups = list(metadata.group_levels) if metadata is not None else []
    min_replicates = _positive_int(params.get("min_replicates"), 2)
    compare_field = str(params.get("compare_field") or "").strip()
    tested = str(params.get("tested_levels") or "").strip()
    reference = str(params.get("reference_level") or "").strip()

    candidates = []
    for group in groups:
        values = [item for item in group.values if item.value.strip()]
        if len(values) != 2 or any(item.count < min_replicates for item in values):
            continue
        observed = {item.value for item in values}
        if compare_field and group.column != compare_field:
            continue
        if tested and tested not in observed:
            continue
        if reference and reference not in observed:
            continue
        refs = [item.value for item in values if _is_reference_level(item.value)]
        if not reference and len(refs) != 1:
            continue
        inferred_reference = reference or refs[0]
        remaining = [item.value for item in values if item.value != inferred_reference]
        inferred_tested = tested or (remaining[0] if len(remaining) == 1 else "")
        if not inferred_tested or inferred_tested == inferred_reference:
            continue
        candidates.append((group.column, inferred_tested, inferred_reference))

    if len(candidates) == 1:
        field, inferred_tested, inferred_reference = candidates[0]
        if not str(params.get("compare_field") or "").strip():
            params["compare_field"] = field
        if not str(params.get("tested_levels") or "").strip():
            params["tested_levels"] = inferred_tested
        if not str(params.get("reference_level") or "").strip():
            params["reference_level"] = inferred_reference
        return params, None

    label = "DEG" if analysis_type is AnalysisType.DIFFERENTIAL else "DEM"
    options = []
    for group in groups[:8]:
        values = "、".join(f"{item.value}({item.count})" for item in group.values[:8] if item.value.strip())
        if values:
            options.append(f"{group.column}=[{values}]")
    option_text = "；".join(options) or "未识别到可用的二水平分组列"
    return params, (
        f"当前输入支持 {label}，但生成可审批计划前需要确认比较设置。"
        f"metadata 中识别到：{option_text}。"
        "请回复比较列、实验组和对照组，例如：比较列=treatment，实验组=salt，对照组=control。"
    )


def _is_reference_level(value: str) -> bool:
    lowered = value.strip().casefold()
    tokens = {item for item in re.split(r"[^a-z0-9\u4e00-\u9fff]+", lowered) if item}
    return lowered in _REFERENCE_LEVEL_MARKERS or bool(tokens & _REFERENCE_LEVEL_MARKERS)


def _positive_int(value: Any, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


_CAPABILITY_HELP = (
    "我可以回答生物学和生物信息学问题，协助准备并运行三类分析："
    "差异表达 DEG（counts + metadata）、差异代谢 DEM（metabs + metadata）和"
    "转录组-代谢组关联 GMA（transcriptome + metabolome + group）。"
    "我也可以进行结果解读：读取你拥有的已完成任务结果，并提供可核验的结果行引用。"
    "实际分析会先检查文件和参数、展示计划，并且只有你明确批准后才创建任务。"
)


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
