from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from typing import Protocol
from uuid import uuid4

from .approvals import ApprovalMismatch, InMemoryApprovalGate
from .audit import AgentEventStore, InMemoryAgentEventStore
from .context import MinimalContextBuilder
from .model import ModelAdapter
from .router import RuleRouter
from .schemas import AgentAction, AgentEvent, AgentState, ActiveProfile, RouteTargetProfile, RunState, RunStatus
from .store import StateNotFound, StateStore
from .validator import DecisionValidator


class RunCoordinator(Protocol):
    """单步运行时接口；本阶段不实现控制循环。"""

    def run_step(self, *, run_id: str, user_id: str, user_message: str) -> RunState:
        ...


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
