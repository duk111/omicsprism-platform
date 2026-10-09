from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from ...analysis_specs import AnalysisSpecRegistry, CapabilityReport
from ..clarification_resolver import (
    ClarificationResolver,
    ClarificationResolverInput,
    ClarificationResolverOutput,
    param_specs_for_analysis,
)
from ..context import ContextAssembler
from ..graph import (
    AgentDecision,
    AgentRole,
    AnalysisModelOutput,
    CapabilityQueryInput,
    GraphState,
    MainDecisionModel,
    PendingAnalysisClarification,
    ToolExecutor,
)
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
        if (
            state.pending_analysis is None
            or state.pending_analysis.status != "active"
            or _looks_like_capability_query(state.user_message)
        ):
            result = loop(state)
            result = _normalize_analysis_response(state, result)
            result = _materialize_human_clarification(state, result)
            if (
                isinstance(result.get("decision"), AgentDecision)
                and result["decision"].action == "capability_query"
            ):
                return _complete_capability_query(state, result)
            return result
        return _resolve_pending_turn(state, resolver)

    return run


def _materialize_human_clarification(
    state: GraphState,
    result: dict[str, object],
) -> dict[str, object]:
    """Persist an analysis agent's uncertainty for the next user turn.

    ``ask_user`` is a semantic HITL outcome, not a terminal protocol detail.
    Keeping it in ``pending_analysis`` lets the next ordinary chat turn enter
    the clarification resolver and continue the same candidate plan.
    """
    decision = result.get("decision")
    if not isinstance(decision, AgentDecision):
        return result
    if decision.action != "ask_user" or state.pending_analysis is not None:
        return result
    question = decision.question or "Please clarify the analysis settings."
    if _has_chinese(state.user_message) != _has_chinese(question):
        question = (
            "请补充分析类型、比较字段、测试组、参考组或范围；"
            "我会先根据当前数据列出候选分析计划。"
            if _has_chinese(state.user_message)
            else "Please provide the analysis type, comparison field, groups, or scope so I can build a candidate plan."
        )
    proposal = decision.proposal
    if proposal is None:
        proposal = AnalysisProposal(analysis_type=decision.analysis_type)
    analysis_type = decision.analysis_type or proposal.analysis_type
    pending = PendingAnalysisClarification(
        analysis_type=analysis_type,
        question=question[:1200],
        missing=["scope"] if "scope" in question.casefold() else [],
        source_message=state.user_message,
        input_bundle_id=state.active_input_bundle_id,
        source_action="propose_plan",
        proposal=proposal,
    )
    result["pending_analysis"] = pending
    result["outcome"] = "needs_input"
    result["failure_code"] = "missing_analysis_parameter"
    result["response_text"] = question[:1200]
    result["response_blocks"] = [text_block(question[:1200])]
    return result


def _normalize_analysis_response(
    state: GraphState,
    result: dict[str, object],
) -> dict[str, object]:
    """Prevent the internal ask_user action from becoming a user-visible protocol."""

    decision = result.get("decision")
    if not isinstance(decision, AgentDecision):
        return result
    # Capability answers are a bounded read-only response.  For every other
    # analysis request, however, the role model is not allowed to terminate
    # the planning protocol with prose (or an internal ask_user action).
    # Push the candidate through the deterministic resolver/validator instead.
    if (
        decision.action == "answer"
        and not _looks_like_capability_query(state.user_message)
        and not _looks_like_readonly_inspection(state.user_message)
        and _looks_like_analysis_request(state.user_message, decision)
    ):
        result["decision"] = AgentDecision(
            action="propose_plan",
            analysis_type=decision.analysis_type,
            proposal=decision.proposal,
        )
        result["response_text"] = None
        result["response_blocks"] = []
        result["outcome"] = None
        result["failure_code"] = None
        return result
    if decision.action != "ask_user":
        return result
    if result.get("outcome") == "failed" or result.get("failure_code"):
        return result
    if state.pending_analysis is not None and state.pending_analysis.status == "active":
        return result
    result["decision"] = AgentDecision(
        action="propose_plan",
        analysis_type=decision.analysis_type,
        proposal=decision.proposal,
    )
    result["response_text"] = None
    result["response_blocks"] = []
    result["outcome"] = None
    result["failure_code"] = None
    return result


def _looks_like_capability_query(message: str) -> bool:
    text = message.casefold().strip()
    return any(marker in text for marker in (
        "能做什么", "可以做什么", "能分析什么", "可以分析什么",
        "哪些分析", "能做哪些分析", "可以做哪些分析",
        "能做差异分析", "可以做差异分析", "what can", "what analyses",
        "can i do", "supported analysis",
    ))


def _has_chinese(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in str(text or ""))


def _looks_like_analysis_request(message: str, decision: AgentDecision) -> bool:
    if decision.analysis_type is not None or decision.proposal is not None:
        return True
    text = message.casefold().strip()
    return any(marker in text for marker in (
        "run ", "analy", "compare ", "contrast", "plan ", "execute", "perform ", "start ",
        "分析", "运行", "比较", "计划", "调控网络", "网络推断", "差异基因", "差异代谢物",
        "regulatory network", "network inference", "multi-omics", "integrated omics",
    ))


def _looks_like_readonly_inspection(message: str) -> bool:
    text = message.casefold().strip()
    return any(marker in text for marker in (
        "describe metadata", "inspect metadata", "metadata fields",
        "enumerate contrast", "list valid contrast", "valid contrast",
        "描述 metadata", "查看 metadata", "列出对比", "有效对比",
    ))


def _complete_capability_query(
    state: GraphState,
    result: dict[str, object],
) -> dict[str, object]:
    decision = result.get("decision")
    if not isinstance(decision, AgentDecision):
        return result
    query = decision.capability_query or CapabilityQueryInput()
    report = AnalysisSpecRegistry().capability_report(
        getattr(profile.profile, "role", "")
        for profile in state.dataset_profiles
    )
    registry = AnalysisSpecRegistry()
    catalog = {item.id: item for item in registry.analysis_catalog()}
    items = report.items
    if query.analysis_type is not None:
        items = [item for item in items if item.analysis_type == query.analysis_type]
    report = CapabilityReport(items=items)
    chinese = any("\u4e00" <= char <= "\u9fff" for char in state.user_message)
    if chinese:
        lines = ["当前数据的分析能力："]
        for item in report.items:
            module = catalog.get(item.analysis_type)
            prefix = (
                f"{item.analysis_type}（{module.label}）：{module.description}；"
                f"所需输入：{', '.join(module.required_inputs)}。"
                if module is not None else f"{item.analysis_type}："
            )
            if item.missing_roles:
                lines.append(
                    f"{prefix}当前缺少输入角色 {', '.join(item.missing_roles)}。"
                )
            else:
                lines.append(f"{prefix}当前输入角色满足，可继续解析分析参数。")
        text = "\n".join(lines) if report.items else "当前没有可评估的分析类型。"
    else:
        lines = ["Analysis capabilities for the current inputs:"]
        for item in report.items:
            module = catalog.get(item.analysis_type)
            prefix = (
                f"{item.analysis_type} ({module.label}): {module.description}; "
                f"required inputs: {', '.join(module.required_inputs)}."
                if module is not None else f"{item.analysis_type}:"
            )
            if item.missing_roles:
                lines.append(
                    f"{prefix} Missing input roles: {', '.join(item.missing_roles)}."
                )
            else:
                lines.append(f"{prefix} Inputs are present; parameters still require resolution.")
        text = "\n".join(lines) if report.items else "No registered analysis is available."
    result.update({
        "decision": AgentDecision(action="answer"),
        "response_text": text[:1200],
        "response_blocks": [text_block(text[:1200])],
        "capability_report": report,
        "outcome": "completed",
        "failure_code": None,
    })
    return result


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
        return {
            "decision": AgentDecision(action="reroute", reroute_to="qa"),
            "response_text": None,
            "response_blocks": [],
            "grounded_answer": None,
            "pending_analysis": pending,
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
            "outcome": "completed",
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
            "outcome": "needs_input",
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
            "outcome": "needs_input",
            "failure_code": "missing_analysis_parameter",
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
    known_fields = contrast_fields | {"scope", "scope_mode", "same_fields"} | set(
        param_specs_for_analysis(base.analysis_type)
    )
    for key, value in patch.items():
        key = aliases.get(str(key), str(key))
        if key not in known_fields:
            raise ValueError(f"不支持修改参数 {key}")
        if key in contrast_fields:
            update[key] = value
        elif key == "scope_mode":
            mode = str(value)
            if mode == "all":
                update["scope"] = ScopeSpec(mode="all")
            elif mode == "stratified":
                current_fields = []
                if isinstance(base.scope, ScopeSpec):
                    current_fields = list(base.scope.blocking_fields)
                update["scope"] = ScopeSpec(mode="stratified", blocking_fields=current_fields or ["__pending_scope_field__"])
            else:
                update["scope"] = ScopeSpec(mode=mode)
        elif key == "same_fields":
            fields = [item.strip() for item in str(value).split(",") if item.strip()]
            update["scope"] = (
                ScopeSpec(mode="stratified", blocking_fields=fields)
                if fields else ScopeSpec(mode="all")
            )
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
        if key in specs and key not in {"compare_field", "tested_level", "reference_level", "same_fields"}
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
