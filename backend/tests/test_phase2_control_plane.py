from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from backend.app.agent.approvals import ApprovalExpired, ApprovalMismatch, InMemoryApprovalGate
from backend.app.agent.context import MinimalContextBuilder
from backend.app.agent.model import ModelBoundaryError, ScriptedModelAdapter
from backend.app.agent.policy import ProfilePolicyGuard, ProfilePolicyViolation
from backend.app.agent.router import RuleRouter
from backend.app.agent.schemas import (
    ActiveProfile,
    AgentAction,
    AgentDecision,
    AgentState,
    Feasibility,
    FeasibilityVerdict,
    RunFocus,
    RunState,
    RunStatus,
    RouteIntent,
    RouteTargetProfile,
    ToolName,
)
from backend.app.agent.store import InMemoryStateStore, StateConflict
from backend.app.agent.validator import DecisionValidator, InvalidDecision


def _state(
    *,
    active_profile: ActiveProfile = ActiveProfile.ANALYSIS,
    state: AgentState = AgentState.COLLECT_INTENT,
    focus_ids: list[str] | None = None,
) -> RunState:
    return RunState(
        run_id="run-1",
        user_id="user-1",
        thread_id="thread-1",
        active_profile=active_profile,
        state=state,
        step_no=0,
        plan_id=None,
        plan_hash=None,
        pending_approval_id=None,
        focus=RunFocus(
            in_scope_job_ids=focus_ids or [],
            resolved_entities={},
            last_citation=None,
        ),
        model_calls=0,
        tool_calls=0,
        status=RunStatus.RUNNING,
        version=0,
    )


def _decision(
    action: AgentAction,
    *,
    requires_approval: bool = False,
    feasibility: Feasibility | None = None,
    params: dict[str, object] | None = None,
) -> AgentDecision:
    return AgentDecision(
        action=action,
        reasoning_summary="fixture decision",
        feasibility=feasibility,
        analysis_recommendations=[],
        requires_approval=requires_approval,
        requested_params=params or {},
    )


def test_router_rules_cover_analysis_interpretation_description_and_unclear() -> None:
    router = RuleRouter()

    assert router.route("我想做差异分析", _state()).target_profile is RouteTargetProfile.ANALYSIS
    assert router.route(
        "我有盐胁迫的转录组和代谢组",
        _state(),
    ).target_profile is RouteTargetProfile.ANALYSIS
    assert router.route(
        "帮我看看这个结果",
        _state(focus_ids=["fixture-job-1"]),
    ).target_profile is RouteTargetProfile.INTERPRETATION
    assert router.route("帮我看看结果", _state()).target_profile is RouteTargetProfile.ASK_USER
    assert router.route(
        "按新参数重跑",
        _state(focus_ids=["fixture-job-1"]),
    ).target_profile is RouteTargetProfile.ANALYSIS
    assert router.route("随便聊聊", _state()).target_profile is RouteTargetProfile.ANALYSIS


def test_router_parameter_answer_and_status_rules_respect_context() -> None:
    """任务 B：宽词规则只在有意义的语境里生效，不吞掉纯咨询意图。"""
    router = RuleRouter()

    # 实验设计咨询（COLLECT_INTENT、draft 为空）不被参数答案规则吞掉。
    consult = "实验组和对照组各设几个重复比较好？"
    assert router.route(consult, _state()).intent is not RouteIntent.ANALYZE
    assert router.route(consult, _state()).intent is RouteIntent.UNCLEAR
    # 同一句话在待补参数时回到计划生成。
    assert router.route(consult, _state(state=AgentState.NEED_USER_INPUT)).intent is RouteIntent.ANALYZE

    # 参数补充答案在协商语境里回到计划生成。
    assert router.route(
        "比较列=treatment，实验组=salt，对照组=control",
        _state(state=AgentState.NEED_USER_INPUT),
    ).intent is RouteIntent.ANALYZE

    # 弱状态词（状态/进度）在没有任务时不是状态查询。
    assert router.route(
        "这个分析的状态机是怎么设计的", _state(),
    ).intent is not RouteIntent.CHECK_STATUS
    # 强状态词即使没有任务也仍是状态查询（由运行时给出正确文案）。
    assert router.route("任务跑完了吗", _state()).intent is RouteIntent.CHECK_STATUS

    # 解读动词优先于状态词。
    assert router.route(
        "结果好了吗，顺便解读一下 top 基因",
        _state(focus_ids=["job-1"]),
    ).intent is RouteIntent.INTERPRET

    # continuation 改为完全匹配：长句不再命中，单字「继续」命中。
    assert router.route("好的，那就继续吧", _state()).intent is RouteIntent.UNCLEAR
    assert router.route("继续", _state()).intent is RouteIntent.ANALYZE


def test_profile_policy_is_structurally_closed() -> None:
    guard = ProfilePolicyGuard()

    guard.authorize(
        user_id="user-1",
        active_profile=ActiveProfile.INTERPRETATION,
        tool=ToolName.QUERY_RESULT_EVIDENCE,
        resource_id="fixture-job-1",
    )
    with pytest.raises(ProfilePolicyViolation):
        guard.authorize(
            user_id="user-1",
            active_profile=ActiveProfile.INTERPRETATION,
            tool=ToolName.SUBMIT_APPROVED_PLAN,
        )
    with pytest.raises(ProfilePolicyViolation):
        guard.authorize(
            user_id="user-1",
            active_profile=ActiveProfile.ANALYSIS,
            tool=ToolName.QUERY_RESULT_EVIDENCE,
        )


def test_approval_requires_matching_unexpired_resumed_plan() -> None:
    gate = InMemoryApprovalGate()
    now = datetime.now(timezone.utc)
    approval_id = gate.suspend(
        run_id="run-1",
        user_id="user-1",
        plan_hash="sha256:plan-a",
        expires_at=now + timedelta(minutes=5),
    )

    assert not gate.is_valid(
        approval_id=approval_id,
        run_id="run-1",
        user_id="user-1",
        plan_hash="sha256:plan-a",
        now=now,
    )
    with pytest.raises(ApprovalMismatch):
        gate.resume(
            approval_id=approval_id,
            run_id="run-1",
            user_id="user-1",
            plan_hash="sha256:plan-b",
            now=now,
        )
    gate.resume(
        approval_id=approval_id,
        run_id="run-1",
        user_id="user-1",
        plan_hash="sha256:plan-a",
        now=now,
    )
    assert gate.is_valid(
        approval_id=approval_id,
        run_id="run-1",
        user_id="user-1",
        plan_hash="sha256:plan-a",
        now=now,
    )

    expired = gate.suspend(
        run_id="run-1",
        user_id="user-1",
        plan_hash="sha256:plan-c",
        expires_at=now - timedelta(seconds=1),
    )
    with pytest.raises(ApprovalExpired):
        gate.resume(
            approval_id=expired,
            run_id="run-1",
            user_id="user-1",
            plan_hash="sha256:plan-c",
            now=now,
        )


def test_approval_survives_gate_reconstruction() -> None:
    shared: dict[str, object] = {}
    now = datetime.now(timezone.utc)
    first_process = InMemoryApprovalGate(shared)
    approval_id = first_process.suspend(
        run_id="run-1",
        user_id="user-1",
        plan_hash="sha256:plan-a",
        expires_at=now + timedelta(minutes=5),
    )

    restarted_process = InMemoryApprovalGate(shared)
    restarted_process.resume(
        approval_id=approval_id,
        run_id="run-1",
        user_id="user-1",
        plan_hash="sha256:plan-a",
        now=now,
    )
    assert restarted_process.is_valid(
        approval_id=approval_id,
        run_id="run-1",
        user_id="user-1",
        plan_hash="sha256:plan-a",
        now=now,
    )
def test_state_store_rejects_stale_writer_and_supports_restart_resume() -> None:
    shared: dict[str, object] = {}
    first_process = InMemoryStateStore(shared)
    first_process.save(_state(), expected_version=0)
    checkpoint = first_process.get(run_id="run-1", user_id="user-1")
    assert checkpoint.version == 1

    with pytest.raises(StateConflict):
        first_process.save(checkpoint, expected_version=0)

    checkpoint.state = AgentState.WAIT_EXECUTION_CONFIRMATION
    checkpoint.status = RunStatus.SUSPENDED
    first_process.save(checkpoint, expected_version=1)

    restarted_process = InMemoryStateStore(shared)
    resumed = restarted_process.get(run_id="run-1", user_id="user-1")
    assert resumed.state is AgentState.WAIT_EXECUTION_CONFIRMATION
    assert resumed.status is RunStatus.SUSPENDED

    attacker_state = resumed.model_copy(update={"user_id": "user-2"})
    with pytest.raises(StateConflict):
        restarted_process.save(attacker_state, expected_version=resumed.version)


def test_context_builder_only_emits_minimal_serializable_context() -> None:
    context = MinimalContextBuilder().build(
        state=_state(focus_ids=["fixture-job-1"]),
        active_profile=ActiveProfile.ANALYSIS,
        user_message="请准备分析计划",
    )

    assert set(context.model_dump()) == {
        "user_message",
        "active_profile",
        "state",
        "in_scope_job_ids",
        "available_result_artifacts",
        "conversation_summary",
        "available_tools",
        "available_input_roles",
        "input_summaries",
        "analysis_capabilities",
        "evidence",
        "confirmed_params",
    }
    assert "database_url" not in context.model_dump()
    assert "raw_file_path" not in context.model_dump()


def test_scripted_model_queue_exhaustion_fails_deterministically() -> None:
    context = MinimalContextBuilder().build(
        state=_state(),
        active_profile=ActiveProfile.ANALYSIS,
        user_message="继续",
    )
    model = ScriptedModelAdapter([])

    with pytest.raises(ModelBoundaryError, match="queue exhausted"):
        model.decide(context)


def test_decision_validator_rejects_invalid_conditional_fields() -> None:
    validator = DecisionValidator()
    with pytest.raises(InvalidDecision):
        validator.validate(
            _state(state=AgentState.CHECK_INPUTS),
            _decision(AgentAction.PROPOSE_PLAN, requires_approval=True),
        )

    with pytest.raises(InvalidDecision):
        validator.validate(
            _state(state=AgentState.ANSWER_WITH_EVIDENCE),
            _decision(AgentAction.PROPOSE_PLAN, feasibility=Feasibility(
                verdict=FeasibilityVerdict.ANSWERABLE,
                reasons=["fixture"],
                missing_information=[],
            )),
        )

    with pytest.raises(InvalidDecision):
        validator.validate(
            _state(state=AgentState.WAIT_PLAN_CONFIRMATION),
            _decision(
                AgentAction.PROPOSE_PLAN,
                feasibility=Feasibility(
                    verdict=FeasibilityVerdict.NOT_ANSWERABLE,
                    reasons=["missing"],
                    missing_information=["metadata"],
                ),
            ),
        )


def test_stub_harness_runs_analysis_approval_resume_and_interpretation_without_jobs() -> None:
    from backend.app.agent.runtime import FixtureRunCoordinator

    model = ScriptedModelAdapter(
        [
            _decision(
                AgentAction.PROPOSE_PLAN,
                requires_approval=True,
                feasibility=Feasibility(
                    verdict=FeasibilityVerdict.ANSWERABLE,
                    reasons=["fixture inputs"],
                    missing_information=[],
                ),
                params={"analysis_type": "differential", "tested_levels": "salt"},
            ),
            _decision(AgentAction.REQUEST_APPROVAL, requires_approval=True),
            _decision(AgentAction.ANSWER),
        ]
    )
    shared: dict[str, object] = {}
    store = InMemoryStateStore(shared)
    coordinator = FixtureRunCoordinator.create(
        state_store=store,
        model=model,
        initial_state=_state(),
    )

    state = coordinator.run_step(run_id="run-1", user_id="user-1", user_message="我想做差异分析")
    assert state.state is AgentState.CHECK_INPUTS

    state = coordinator.run_step(run_id="run-1", user_id="user-1", user_message="确认计划")
    assert state.state is AgentState.WAIT_PLAN_CONFIRMATION
    assert state.plan_hash

    state = coordinator.run_step(run_id="run-1", user_id="user-1", user_message="请求审批")
    assert state.status is RunStatus.SUSPENDED
    assert state.pending_approval_id

    with pytest.raises(ApprovalMismatch):
        coordinator.run_step(run_id="run-1", user_id="user-1", user_message="先解释一下")
    unchanged = store.get(run_id="run-1", user_id="user-1")
    assert unchanged.status is RunStatus.SUSPENDED
    assert unchanged.step_no == 3
    assert coordinator.created_job_ids == []

    state = coordinator.run_step(run_id="run-1", user_id="user-1", user_message="批准执行")
    assert state.status is RunStatus.RUNNING
    assert state.active_profile is ActiveProfile.INTERPRETATION
    assert state.focus.in_scope_job_ids == ["fixture-job-1"]

    state = coordinator.run_step(run_id="run-1", user_id="user-1", user_message="查看结果")
    assert state.state is AgentState.AWAIT_FOLLOWUP
    assert coordinator.created_job_ids == []
    assert state.step_no == 5
    assert state.model_calls == 3


def test_bounded_consultation_is_not_mistaken_for_approval_suspend() -> None:
    from backend.app.agent.runtime import FixtureRunCoordinator

    model = ScriptedModelAdapter([])
    store = InMemoryStateStore({})
    coordinator = FixtureRunCoordinator.create(
        state_store=store,
        model=model,
        initial_state=_state(),
    )

    state = coordinator.run_step(run_id="run-1", user_id="user-1", user_message="随便聊聊")
    assert state.state is AgentState.CHECK_INPUTS
    assert state.status is RunStatus.RUNNING
