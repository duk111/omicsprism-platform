from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from ..clarification_resolver import (
    ClarificationResolver,
    ClarificationResolverInput,
    ClarificationResolverOutput,
    param_specs_for_analysis,
)
from ..context import ContextAssembler
from ..graph import AgentDecision, AgentRole, AnalysisModelOutput, GraphState, MainDecisionModel, ToolExecutor
from ..message_blocks import text_block
from ..param_resolver import AnalysisProposal, ContrastSpec, GMAParams, DEGParams, DEMParams, ScopeSpec
from ..schemas import ToolName
from ..trace import TraceRecorder
from .main import (
    _MODEL_FALLBACK_QUESTION,
    _advance_model_budget,
    _ask_user_update,
    _run_agent_loop,
)


def analysis_agent_node(
    model: MainDecisionModel,
    tool_executor: ToolExecutor | None = None,
    trace_recorder: TraceRecorder | None = None,
    clarification_resolver: ClarificationResolver | object | None = None,
) -> Callable[[GraphState], dict[str, object]]:
    loop = _run_agent_loop(
        AgentRole.ANALYSIS,
        model,
        tool_executor,
        trace_recorder,
        ContextAssembler().assemble_for_analysis,
        AnalysisModelOutput,
        {ToolName.DESCRIBE_METADATA, ToolName.ENUMERATE_CONTRASTS},
    )
    resolver = (
        clarification_resolver
        if isinstance(clarification_resolver, ClarificationResolver)
        else ClarificationResolver(clarification_resolver or model)
    )

    def run(state: GraphState) -> dict[str, object]:
        if state.pending_analysis is None or state.pending_analysis.status != "active":
            return loop(state)
        return _resolve_pending_turn(state, resolver)

    return run


def _resolve_pending_turn(
    state: GraphState,
    resolver: ClarificationResolver,
) -> dict[str, object]:
    pending = state.pending_analysis
    if pending is None or pending.status != "active":
        raise ValueError("pending analysis is not active")
    if (
        state.step_budget.used_model_steps >= state.step_budget.max_model_steps
        or state.step_budget.used_tokens >= state.step_budget.max_tokens
    ):
        return _ask_user_update(
            "当前请求已达到执行步数上限，请重新描述你要完成的操作。",
            state.step_budget,
        )
    request = ClarificationResolverInput(
        user_reply=state.user_message,
        pending_question=pending.question,
        options=pending.options,
        param_spec=param_specs_for_analysis(pending.analysis_type),
        recent_messages=list(state.recent_messages.messages),
    )
    result = resolver(request)
    budget = _advance_model_budget(state.step_budget, getattr(resolver, "last_usage", None))

    if result.intent == "unrelated":
        if state.reroute_count >= 2:
            return _ask_user_update(_MODEL_FALLBACK_QUESTION, budget)
        return {
            "decision": AgentDecision(action="reroute", reroute_to="qa"),
            "response_text": None,
            "response_blocks": [],
            "grounded_answer": None,
            "pending_analysis": pending,
            "reroute_count": state.reroute_count + 1,
            "step_budget": budget,
        }
    if result.intent == "cancel":
        cancelled = pending.model_copy(update={"status": "cancelled"})
        text = "已取消当前分析，不会提交任务。"
        return {
            "decision": AgentDecision(action="ask_user", question=text),
            "response_text": text,
            "response_blocks": [text_block(text)],
            "grounded_answer": None,
            "pending_analysis": cancelled,
            "step_budget": budget,
        }
    if result.intent == "param_question":
        text = result.answer_text or "请指出你想了解的参数名称。"
        return {
            "decision": AgentDecision(action="ask_user", question=text),
            "response_text": text,
            "response_blocks": [text_block(text)],
            "grounded_answer": None,
            "pending_analysis": pending,
            "step_budget": budget,
        }

    try:
        proposal = _proposal_for_pending(pending, result)
        _validate_patch_shape(proposal)
    except (TypeError, ValueError, ValidationError) as exc:
        text = f"参数修改未通过校验：{str(exc)[:700]}。请重新说明要修改的参数。"
        return {
            "decision": AgentDecision(action="ask_user", question=text),
            "response_text": text,
            "response_blocks": [text_block(text)],
            "grounded_answer": None,
            "pending_analysis": pending,
            "step_budget": budget,
        }

    decision = AgentDecision(
        action=pending.source_action,
        analysis_type=proposal.analysis_type or pending.analysis_type,
        proposal=proposal,
    )
    updated_pending = pending.model_copy(update={
        "proposal": proposal,
        "status": "consumed" if result.intent == "confirm" else pending.status,
    })
    return {
        "decision": decision,
        "response_text": None,
        "response_blocks": [],
        "grounded_answer": None,
        "pending_analysis": updated_pending,
        "step_budget": budget,
    }


def _proposal_for_pending(
    pending: Any,
    result: ClarificationResolverOutput,
) -> AnalysisProposal:
    base = pending.proposal or AnalysisProposal(analysis_type=pending.analysis_type)
    patch: dict[str, object] = {}
    if result.matched_option_id:
        option = next(
            (item for item in pending.options if item.option_id == result.matched_option_id),
            None,
        )
        if option is None:
            raise ValueError("候选 option_id 不属于当前澄清问题")
        patch.update(option.proposal_patch)
    patch.update(result.proposal_patch)
    requested = dict(base.requested_params)
    update: dict[str, object] = {}
    contrast_fields = {"compare_field", "tested_level", "reference_level"}
    aliases = {"tested_levels": "tested_level", "reference": "reference_level"}
    known_fields = contrast_fields | {"scope", "scope_mode"} | set(
        param_specs_for_analysis(base.analysis_type)
    )
    for key, value in patch.items():
        key = aliases.get(str(key), str(key))
        if key not in known_fields:
            raise ValueError(f"不支持修改参数 {key}")
        if key in contrast_fields:
            update[key] = value
        elif key == "scope_mode":
            update["scope"] = ScopeSpec(mode=str(value))
        elif key == "scope":
            update["scope"] = ScopeSpec.model_validate(value)
        else:
            requested[key] = value  # type: ignore[assignment]
    if update.get("scope") is None and "scope" not in update:
        update.pop("scope", None)
    update["requested_params"] = requested
    return base.model_copy(update=update)


def _validate_patch_shape(proposal: AnalysisProposal) -> None:
    """Validate the fields available before the full metadata resolver runs."""

    analysis_type = proposal.analysis_type
    if analysis_type not in {"DEG", "DEM", "GMA"}:
        raise ValueError("analysis_type is required")
    specs = param_specs_for_analysis(analysis_type)
    scalar = {
        key: value
        for key, value in proposal.requested_params.items()
        if key in specs
    }
    if analysis_type == "GMA":
        GMAParams.model_validate({"analysis_type": "GMA", **scalar})
        return
    compare_field = proposal.compare_field or "__pending_compare_field__"
    tested_level = proposal.tested_level or "__pending_tested_level__"
    reference_level = proposal.reference_level or "__pending_reference_level__"
    contrast = ContrastSpec(
        compare_field=compare_field,
        tested_level=tested_level,
        reference_level=reference_level,
        scope=proposal.scope,
    )
    model = DEGParams if analysis_type == "DEG" else DEMParams
    model.model_validate({
        "analysis_type": analysis_type,
        "contrast": contrast,
        **scalar,
    })


__all__ = ["analysis_agent_node"]
