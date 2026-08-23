from __future__ import annotations

# DEPRECATED-BY: phase-5
# This file characterizes the RuleRouter/ModelRouter control-plane path.

from backend.app.agent.router import ModelRouter, RuleRouter
from backend.app.agent.schemas import (
    ActiveProfile,
    AgentState,
    RouteIntent,
    RouteTargetProfile,
    RunFocus,
    RunState,
    RunStatus,
)


def _state(*, jobs: list[str] | None = None, inputs: bool = False, pending: bool = False) -> RunState:
    return RunState(
        run_id="run-1", user_id="user-1", thread_id="thread-1",
        active_profile=ActiveProfile.ANALYSIS, state=AgentState.WAIT_EXECUTION_CONFIRMATION if pending else AgentState.COLLECT_INTENT,
        step_no=0, plan_id="plan-1" if pending else None, plan_hash="sha256:plan" if pending else None,
        pending_approval_id="approval-1" if pending else None,
        focus=RunFocus(in_scope_job_ids=jobs or [], resolved_entities={}, last_citation=None,
                       params_source_ref="source" if inputs else None),
        model_calls=0, tool_calls=0, status=RunStatus.SUSPENDED if pending else RunStatus.RUNNING, version=0,
    )


def _route(**overrides):
    payload = {
        "intent": "analyze", "target_profile": "analysis", "is_param_negotiation": False,
        "confidence": "high", "reason": "model route",
    }
    payload.update(overrides)
    return payload


def test_invalid_model_route_falls_back_to_rule_router() -> None:
    router = ModelRouter(lambda _message, _state: {"intent": "invalid"})
    expected = RuleRouter().route("我想做差异分析", _state())
    actual = router.route("我想做差异分析", _state())
    assert actual.intent is expected.intent
    assert actual.target_profile is expected.target_profile


def test_low_confidence_model_route_falls_back() -> None:
    router = ModelRouter(lambda _message, _state: _route(confidence="low", intent="interpret", target_profile="interpretation"))
    actual = router.route("我想做差异分析", _state())
    assert actual.intent is RouteIntent.ANALYZE
    assert actual.target_profile is RouteTargetProfile.ANALYSIS


def test_interpret_without_focus_job_is_post_checked_to_ask_user() -> None:
    router = ModelRouter(lambda _message, _state: _route(intent="interpret", target_profile="interpretation"))
    actual = router.route("解释结果", _state(jobs=[]))
    assert actual.intent is RouteIntent.INTERPRET
    assert actual.target_profile is RouteTargetProfile.ASK_USER


def test_analyze_without_inputs_is_post_checked_to_describe_only() -> None:
    router = ModelRouter(lambda _message, _state: _route(), has_inputs=lambda _state: False)
    actual = router.route("run differential expression", _state(inputs=False))
    assert actual.intent is RouteIntent.DESCRIBE_ONLY
    assert actual.target_profile is RouteTargetProfile.ANALYSIS


def test_pending_param_negotiation_requires_rule_confirmation() -> None:
    router = ModelRouter(lambda _message, _state: _route(is_param_negotiation=True))
    actual = router.route("这个计划是什么意思", _state(pending=True))
    assert actual.intent is RouteIntent.ANALYZE
    assert actual.is_param_negotiation is False


def test_pending_explicit_parameter_change_can_be_confirmed_by_rule_router() -> None:
    router = ModelRouter(lambda _message, _state: _route(is_param_negotiation=True))
    actual = router.route("padj 改成 0.01", _state(pending=True))
    assert actual.is_param_negotiation is True


def test_model_adapter_route_context_has_no_tools() -> None:
    seen = {}

    class Adapter:
        def decide(self, context):
            seen["tools"] = list(context.available_tools)
            return _route(intent="analyze", target_profile="analysis")

    router = ModelRouter(model=Adapter())
    actual = router.route("run differential expression", _state(inputs=True))
    assert actual.intent is RouteIntent.ANALYZE
    assert seen["tools"] == []
