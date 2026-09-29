from __future__ import annotations

import json
import logging
from copy import deepcopy
from time import perf_counter
from collections.abc import Callable
from typing import Literal
from pydantic import BaseModel

from pydantic import ValidationError

from ..grounding import GroundedAnswerPipeline
from ..graph import (
    AgentDecision,
    AgentRole,
    AnalysisModelOutput,
    GraphState,
    MainDecisionModel,
    AgentLoopOutput,
    QaModelOutput,
    ResultQaModelOutput,
    RouteTarget,
    StepBudget,
    ToolCallRequest,
    ToolExecutor,
    ToolObservation,
)
from ..context import ContextAssembler, AgentControlContext
from ..schemas import AgentEvidenceBlock, GroundedAnswer, ToolName, ToolResult
from ..message_blocks import text_block
from ..trace import TraceRecorder, stable_hash
from ..router import _is_explicit_jobs_listing as _router_is_explicit_jobs_listing


_MODEL_FALLBACK_QUESTION = (
    "我暂时无法可靠判断你的意图。请说明你是想了解一般知识、运行分析，"
    "还是查询已有任务或结果。"
)
_STEP_BUDGET_QUESTION = "当前请求已达到执行步数上限，请重新描述你要完成的操作。"
_MODEL_RETRY_INSTRUCTION = (
    "上一次结构化响应未通过校验。请重新输出完整对象：如果 action=answer，"
    "必须填写非空 answer；如果 action=ask_user，必须填写非空 question；"
    "其他 action 的 answer 必须为 null。"
)
_FOLLOWUP_RETRY_INSTRUCTION = (
    "The user is explicitly asking to revise the previous assistant answer. "
    "Use recent_messages, answer the requested revision directly, and do not ask "
    "for dataset details unless the user introduced a genuinely new missing fact."
)


LOG = logging.getLogger("omicsprism.platform.agent_main")
_TOOL_SUMMARY_MAX_CHARS = 3900


def _run_agent_loop(
    role: object,
    model: MainDecisionModel,
    tool_executor: ToolExecutor | None = None,
    trace_recorder: TraceRecorder | None = None,
    context_builder: Callable[[GraphState], object] | None = None,
    output_model: type[BaseModel] = AgentLoopOutput,
    allowed_tools: set[ToolName] | None = None,
) -> Callable[[GraphState], dict[str, object]]:
    effective_context_builder = context_builder
    effective_output_model = output_model
    effective_allowed_tools = allowed_tools or set()

    def loop_run(state: GraphState) -> dict[str, object]:
        budget = state.step_budget
        observations = list(state.tool_observations)
        working_state = state
        repetition_guidance: str | None = None
        terminal_only = False
        pipeline = GroundedAnswerPipeline()
        latest_evidence: ToolResult | None = None
        while True:
            if (
                budget.used_model_steps >= budget.max_model_steps
                or budget.used_tokens >= budget.max_tokens
            ):
                return _ask_user_update(
                    _budget_question(observations),
                    budget,
                    observations,
                )
            context = (
                effective_context_builder(working_state)
                if effective_context_builder is not None
                else _control_context(working_state)
            )
            control_context = _control_context(working_state)
            if repetition_guidance is not None:
                if "tool_repetition_guidance" in context.model_fields:
                    context = context.model_copy(update={
                        "tool_repetition_guidance": repetition_guidance,
                    })
                control_context = control_context.model_copy(update={
                    "tool_repetition_guidance": repetition_guidance,
                })
            output = None
            model_failure_code = "model_unavailable"
            for _attempt in range(2):
                if (
                    budget.used_model_steps >= budget.max_model_steps
                    or budget.used_tokens >= budget.max_tokens
                ):
                    break
                try:
                    attempt_context = context
                    if _attempt:
                        summary = control_context.conversation_summary or ""
                        retry_instruction = (
                            _FOLLOWUP_RETRY_INSTRUCTION
                            if _should_retry_followup(control_context)
                            else _MODEL_RETRY_INSTRUCTION
                        )
                        attempt_context = context.model_copy(update={
                            "conversation_summary": (
                                f"{summary}\n{retry_instruction}"
                                if summary else retry_instruction
                            )[:1200],
                        }) if "conversation_summary" in context.model_fields else context
                    candidate = _invoke_model_output(
                        model,
                        attempt_context,
                        role=role,
                        output_model=effective_output_model,
                    )
                except (Exception, ValidationError) as exc:
                    if isinstance(exc, ValidationError):
                        model_failure_code = "role_schema_validation_failed"
                    LOG.warning(
                        "model decision rejected",
                        extra={
                            "event": "agent.model.rejected",
                            "error_code": type(exc).__name__,
                        },
                        exc_info=True,
                    )
                    budget = _advance_model_budget(budget, getattr(model, "last_usage", None))
                    continue
                usage = getattr(model, "last_usage", None)
                budget = _advance_model_budget(budget, usage)
                if terminal_only and candidate.decision.action not in {
                    "answer",
                    "ask_user",
                    "grounded_answer",
                }:
                    LOG.warning(
                        "model selected a non-terminal action after repeated deterministic result",
                        extra={"event": "agent.routing.repeated_tool_blocked"},
                    )
                    if _attempt == 0:
                        continue
                    return _ask_user_update(
                        _MODEL_FALLBACK_QUESTION,
                        budget,
                        observations,
                        outcome="unresolved",
                        failure_code="route_ambiguous",
                    )
                if (
                    _attempt == 0
                    and candidate.decision.action == "ask_user"
                    and _should_retry_followup(context)
                ):
                    # A clarification is a valid schema response, but is a
                    # semantic mismatch for an explicit rewrite of the prior
                    # answer. Give the model one bounded correction attempt.
                    LOG.info(
                        "retrying explicit answer follow-up after model clarification",
                        extra={"event": "agent.model.followup_retry"},
                    )
                    continue
                output = candidate
                break
            if output is None:
                return _ask_user_update(
                    _MODEL_FALLBACK_QUESTION,
                    budget,
                    observations,
                    outcome="failed",
                    failure_code=model_failure_code,
                )

            decision = output.decision
            if (
                decision.action == "tool_call"
                and effective_allowed_tools is not None
                and decision.tool not in effective_allowed_tools
            ):
                return _ask_user_update(
                    "The requested tool is not available for this agent role.",
                    budget,
                    observations,
                    outcome="failed",
                    failure_code="tool_call_rejected",
                )
            native_call_id = _native_tool_call_id(model, decision) or f"call-{len(observations) + 1}"
            native_assistant_message = getattr(model, "last_assistant_message", None) or {}
            if (
                not any(observation.tool is ToolName.LIST_JOBS for observation in observations)
                and _should_force_list_jobs(context, decision, tool_executor)
            ):
                LOG.info(
                    "forcing list_jobs tool for explicit jobs listing request",
                    extra={"event": "agent.routing.list_jobs_guard"},
                )
                decision = AgentDecision(
                    action="tool_call",
                    tool=ToolName.LIST_JOBS,
                    arguments={},
                )
                native_call_id = None
                native_assistant_message = {}
            if (
                decision.action == "tool_call"
                and observations
                and observations[-1].tool is decision.tool
                and observations[-1].arguments_hash == _arguments_hash(decision.arguments)
            ):
                if observations[-1].retryable and observations[-1].retry_count == 0:
                    if tool_executor is None:
                        return _ask_user_update(
                            "I cannot access the requested read-only data tool in this runtime.",
                            budget,
                            observations,
                        )
                    if budget.used_tool_calls >= budget.max_tool_calls:
                        return _ask_user_update(
                            _budget_question(observations),
                            budget,
                            observations,
                        )
                    request = ToolCallRequest(
                        tool=decision.tool,
                        arguments=decision.arguments,
                    )
                    summary, tool_outcome, retryable, tool_error_code, evidence = _execute_tool_request(
                        request,
                        working_state,
                        tool_executor,
                        trace_recorder,
                    )
                    if evidence is not None:
                        latest_evidence = evidence
                    observations.append(ToolObservation(
                        tool=request.tool,
                        call_id=native_call_id or f"call-{len(observations) + 1}",
                        arguments=dict(request.arguments),
                        assistant_message=dict(native_assistant_message),
                        summary=summary,
                        arguments_hash=_arguments_hash(request.arguments),
                        outcome=tool_outcome,
                        retryable=retryable,
                        retry_count=1,
                    ))
                    observations = observations[-12:]
                    budget = _advance_tool_budget(budget)
                    if tool_outcome == "failed" and (
                        not retryable or observations[-1].retry_count >= 1
                    ):
                        return _ask_user_update(
                            "The requested data tool failed and could not be retried.",
                            budget,
                            observations,
                            outcome="failed",
                            failure_code="tool_execution_failed",
                        )
                    if budget.used_tool_calls >= budget.max_tool_calls:
                        return _ask_user_update(
                            _budget_question(observations),
                            budget,
                            observations,
                        )
                    working_state = state.model_copy(update={
                        "tool_observations": observations,
                        "step_budget": budget,
                    })
                    continue
                if decision.tool is ToolName.LIST_JOBS and _router_is_explicit_jobs_listing(control_context.user_message):
                    response_text = _list_jobs_response(observations[-1].summary)
                    return {
                        "decision": AgentDecision(action="answer"),
                        "response_text": response_text,
                        "response_blocks": [text_block(response_text)],
                        "grounded_answer": None,
                        "step_budget": budget,
                        "tool_observations": observations,
                    }
                if decision.tool is ToolName.QUERY_ARTIFACT and latest_evidence is not None:
                    answer = pipeline.answer(latest_evidence, draft=None, repair=None)
                    response_text = _grounded_answer_text(answer)
                    return {
                        "decision": AgentDecision(action="grounded_answer"),
                        "grounded_answer": answer,
                        "response_text": response_text,
                        "response_blocks": [
                            text_block(response_text),
                            AgentEvidenceBlock(claims=answer.claims),
                        ],
                        "step_budget": budget,
                        "tool_observations": observations,
                    }
                if decision.tool in {
                    ToolName.DESCRIBE_METADATA,
                    ToolName.ENUMERATE_CONTRASTS,
                }:
                    response_text = _read_only_observation_response(
                        decision.tool, observations[-1].summary
                    )
                    return {
                        "decision": AgentDecision(action="answer"),
                        "response_text": response_text,
                        "response_blocks": [text_block(response_text)],
                        "grounded_answer": None,
                        "step_budget": budget,
                        "tool_observations": observations,
                    }
                repetition_guidance = _repeated_tool_guidance(
                    decision.tool,
                    observations[-1].summary,
                )
                terminal_only = True
                continue
            if decision.action == "grounded_answer":
                if latest_evidence is None:
                    return _ask_user_update(
                        "I need a verified result artifact before I can answer this question.",
                        budget,
                        observations,
                    )
                answer = pipeline.answer(
                    latest_evidence,
                    draft=decision.grounded_answer,
                    repair=None,
                )
                return {
                    "decision": decision,
                    "grounded_answer": answer,
                    "response_text": _grounded_answer_text(answer),
                    "response_blocks": [
                        text_block(_grounded_answer_text(answer)),
                        AgentEvidenceBlock(claims=answer.claims),
                    ],
                    "step_budget": budget,
                    "tool_observations": observations,
                }
            if (
                decision.action != "tool_call"
                and observations
                and observations[-1].outcome == "failed"
            ):
                return _ask_user_update(
                    "The requested data tool failed before a verified answer could be produced.",
                    budget,
                    observations,
                    outcome="failed",
                    failure_code="tool_execution_failed",
                )
            if decision.action != "tool_call":
                response_text = _response_text(output)
                return {
                    "decision": decision,
                    "response_text": response_text,
                    "response_blocks": (
                        [text_block(response_text)] if response_text is not None else []
                    ),
                    "grounded_answer": decision.grounded_answer,
                    "outcome": (
                        "needs_input"
                        if decision.action == "ask_user"
                        and getattr(role, "value", role) == "analysis"
                        else "unresolved"
                        if decision.action == "ask_user"
                        else "completed"
                    ),
                    "failure_code": (
                        "missing_analysis_parameter"
                        if decision.action == "ask_user"
                        and getattr(role, "value", role) == "analysis"
                        else "route_ambiguous"
                        if decision.action == "ask_user"
                        else None
                    ),
                    "step_budget": budget,
                    "tool_observations": observations,
                }
            if tool_executor is None:
                return _ask_user_update(
                    "I cannot access the requested read-only data tool in this runtime.",
                    budget,
                    observations,
                    outcome="failed",
                    failure_code="tool_call_rejected",
                )
            if budget.used_tool_calls >= budget.max_tool_calls:
                return _ask_user_update(
                    _budget_question(observations),
                    budget,
                    observations,
                )
            request = ToolCallRequest(
                tool=decision.tool,
                arguments=decision.arguments,
            )
            summary, tool_outcome, retryable, tool_error_code, evidence = _execute_tool_request(
                request,
                working_state,
                tool_executor,
                trace_recorder,
            )
            if evidence is not None:
                latest_evidence = evidence
            observations.append(ToolObservation(
                tool=request.tool,
                call_id=native_call_id or f"call-{len(observations) + 1}",
                arguments=dict(request.arguments),
                assistant_message=dict(native_assistant_message),
                summary=summary,
                arguments_hash=_arguments_hash(request.arguments),
                outcome=tool_outcome,
                retryable=retryable,
                retry_count=0,
            ))
            observations = observations[-12:]
            budget = _advance_tool_budget(budget)
            if tool_outcome == "failed" and not retryable:
                return _ask_user_update(
                    "The requested data tool failed.",
                    budget,
                    observations,
                    outcome="failed",
                    failure_code="tool_execution_failed",
                )
            if budget.used_tool_calls >= budget.max_tool_calls:
                return _ask_user_update(
                    _budget_question(observations),
                    budget,
                    observations,
                )
            working_state = state.model_copy(update={
                "tool_observations": observations,
                "step_budget": budget,
            })

    def run(state: GraphState) -> dict[str, object]:
        return loop_run(state)

    return run


def _invoke_model_output(
    model: MainDecisionModel,
    context: object,
    *,
    role: object,
    output_model: type[BaseModel],
) -> AgentLoopOutput:
    """Call a role-specific model and enforce that role's output schema."""

    role_output = output_model.model_validate(model(context, role=role))
    return _coerce_role_output(role_output)


def _coerce_role_output(output: object) -> AgentLoopOutput:
    if isinstance(output, (QaModelOutput, AnalysisModelOutput, ResultQaModelOutput)):
        return AgentLoopOutput(
            decision=AgentDecision(**output.decision.model_dump(mode="python")),
            answer=output.answer,
        )
    return AgentLoopOutput.model_validate(output)


def route_node(state: GraphState, route_model: MainDecisionModel | None = None) -> dict[str, object]:
    """Resolve one route target, classify only uncertain data-related requests."""

    from ..router import route
    from ..graph import RouteClassification, RouteDecision, RouteTarget

    target = route(state)
    source = "reroute" if state.decision is not None and state.decision.action == "reroute" else "rule"
    reason = "deterministic route rule"
    if target is None:
        source = "classifier"
        reason = "semantic classification required"
        try:
            if route_model is None:
                raise ValueError("route classifier is unavailable")
            classification_context = ContextAssembler().assemble_for_router(state)
            classification = RouteClassification.model_validate(
                route_model(classification_context, role=AgentRole.ROUTER)
            )
            if classification.confidence < 0.72:
                target = RouteTarget.AMBIGUOUS
                reason = "classifier confidence below threshold"
            else:
                target = RouteTarget(classification.target)
        except ValidationError:
            return _route_terminal(
                state,
                outcome="failed",
                failure_code="role_schema_validation_failed",
                question="我无法可靠判断这条请求的处理路径，请重新说明你的目标。",
                source=source,
                reason="classifier output failed schema validation",
            )
        except Exception:
            return _route_terminal(
                state,
                outcome="failed",
                failure_code="model_unavailable",
                question="意图分类服务暂时不可用，请稍后重试。",
                source=source,
                reason="classifier invocation failed",
            )
    if target is None:
        target = RouteTarget.AMBIGUOUS
    else:
        target = RouteTarget(target.value if isinstance(target, AgentRole) else target)
    route_decision = RouteDecision(
        target=target,
        source=source,
        reason=reason,
    )
    if target is RouteTarget.AMBIGUOUS:
        return _route_terminal(
            state,
            outcome="unresolved",
            failure_code="route_ambiguous",
            question="我还不能可靠判断这条请求属于哪类操作。你可以说明是解释概念、评估数据能力、开始分析，还是查询已有结果。",
            source=source,
            reason=reason,
            route_decision=route_decision,
        )
    if target is RouteTarget.UNSUPPORTED:
        return _route_terminal(
            state,
            outcome="unsupported",
            failure_code="unsupported_request",
            question="当前平台不支持这类请求。",
            source=source,
            reason=reason,
            route_decision=route_decision,
        )
    role = AgentRole(target.value)
    visited = list(state.visited_roles)
    if role in visited or len(visited) >= 3:
        text = "我暂时无法可靠完成这次请求，请重新描述你要解释、分析或查询的内容。"
        return {
            "decision": AgentDecision(action="ask_user", question=text),
            "response_text": text,
            "response_blocks": [text_block(text)],
            "outcome": "unresolved",
            "failure_code": "route_exhausted",
            "route_decision": RouteDecision(
                target=RouteTarget.AMBIGUOUS,
                source=source,
                reason="route target already visited or hop limit reached",
            ),
        }
    visited.append(role)
    return {
        "visited_roles": visited,
        "route_decision": route_decision,
    }


def _route_terminal(
    state: GraphState,
    *,
    outcome: Literal["failed", "unsupported", "unresolved"],
    failure_code: str,
    question: str,
    source: str,
    reason: str,
    route_decision: object | None = None,
) -> dict[str, object]:
    from ..graph import RouteDecision, RouteTarget

    return {
        "decision": AgentDecision(action="ask_user", question=question),
        "response_text": question,
        "response_blocks": [text_block(question)],
        "outcome": outcome,
        "failure_code": failure_code,
        "route_decision": route_decision or RouteDecision(
            target=(
                RouteTarget.UNSUPPORTED if outcome == "unsupported"
                else RouteTarget.AMBIGUOUS
            ),
            source=source,  # type: ignore[arg-type]
            reason=reason,
        ),
    }


def route_after_route(state: GraphState) -> Literal["qa", "analysis", "result_qa", "end"]:
    if state.route_decision is None or state.route_decision.target in {
        RouteTarget.AMBIGUOUS,
        RouteTarget.UNSUPPORTED,
    }:
        return "end"
    return state.route_decision.target.value  # type: ignore[return-value]


def route_after_agent(
    state: GraphState,
) -> Literal["analysis", "result_qa", "end", "route"]:
    decision = state.decision
    if decision is None:
        return "end"
    if decision.action == "reroute":
        return "route"
    if decision.action in {"inspect_dataset", "run_analysis", "propose_plan"}:
        return "analysis"
    if decision.action in {"get_job", "query_result"}:
        return "result_qa"
    return "end"


def _control_context(state: GraphState) -> AgentControlContext:
    return ContextAssembler().assemble_control(state)


def _arguments_hash(arguments: dict[str, object]) -> str:
    return stable_hash(arguments)


def _native_tool_call_id(model: object, decision: AgentDecision) -> str | None:
    """Read the model-provided call id when using native tool calling."""

    if decision.action != "tool_call":
        return None
    calls = getattr(model, "last_tool_calls", None)
    if not isinstance(calls, list) or not calls:
        return None
    call = calls[0]
    call_id = call.get("id") if isinstance(call, dict) else None
    return call_id.strip() if isinstance(call_id, str) and call_id.strip() else None


def _repeated_tool_guidance(tool: ToolName, summary: str) -> str:
    return (
        f"You already called {tool.value} with the same arguments. Do not call any tool "
        "again. Use the complete result below to answer the user directly, or ask the "
        "user to clarify a genuinely missing detail.\n"
        f"Complete tool result summary:\n{summary}"
    )


def _execute_tool_request(
    request: ToolCallRequest,
    state: GraphState,
    tool_executor: ToolExecutor,
    trace_recorder: TraceRecorder | None,
) -> tuple[str, Literal["ok", "failed"], bool, str | None, ToolResult | None]:
    tool_started = perf_counter()
    tool_outcome: Literal["ok", "failed"] = "ok"
    retryable = False
    tool_error_code: str | None = None
    evidence: ToolResult | None = None
    try:
        result = tool_executor(request, state)
        summary = _serialize_tool_result(result)
        evidence = _as_grounding_evidence(result)
        if isinstance(result, dict):
            result_ok = result.get("ok", True)
            result_error_code = result.get("error_code")
        else:
            result_ok = getattr(result, "ok", True)
            result_error_code = getattr(result, "error_code", None)
        if result_ok is False:
            tool_outcome = "failed"
            retryable = _is_transient_error_code(result_error_code)
            if isinstance(result_error_code, str) and result_error_code:
                tool_error_code = result_error_code
    except Exception as exc:
        summary = "tool execution failed"
        tool_outcome = "failed"
        retryable = _is_transient_tool_error(exc)
        tool_error_code = type(exc).__name__
    if trace_recorder is not None:
        trace_recorder.tool_call(
            context=state,
            tool_name=request.tool.value,
            tool_schema_hash=stable_hash(ToolCallRequest.model_json_schema()),
            latency_ms=round((perf_counter() - tool_started) * 1000, 3),
            outcome=tool_outcome,
            failure_code="tool_execution_failed" if tool_outcome == "failed" else None,
        )
    return summary, tool_outcome, retryable, tool_error_code, evidence


def _is_transient_tool_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status_code is None and response is not None:
        status_code = getattr(response, "status_code", None)
    if isinstance(status_code, int) and 500 <= status_code <= 599:
        return True
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    name = type(exc).__name__.casefold()
    return "timeout" in name or "connect" in name


def _is_transient_error_code(error_code: object) -> bool:
    if not isinstance(error_code, str):
        return False
    normalized = error_code.casefold()
    return (
        "timeout" in normalized
        or "temporar" in normalized
        or "unavailable" in normalized
        or any(code in normalized for code in ("500", "502", "503", "504", "429"))
    )


def _ask_user_update(
    question: str,
    budget: StepBudget,
    observations: list[ToolObservation] | None = None,
    *,
    outcome: Literal["needs_input", "unsupported", "unresolved", "failed"] = "needs_input",
    failure_code: str | None = None,
) -> dict[str, object]:
    if failure_code is None:
        failure_code = {
            "needs_input": "missing_analysis_parameter",
            "unsupported": "unsupported_request",
            "unresolved": "route_ambiguous",
            "failed": "tool_execution_failed",
        }[outcome]
    return {
        "decision": AgentDecision(
            action="ask_user",
            question=question,
            decision_note="deterministic model fallback",
        ),
        "grounded_answer": None,
        "response_text": question,
        "response_blocks": [text_block(question)],
        "outcome": outcome,
        "failure_code": failure_code,
        "step_budget": budget,
        "tool_observations": observations or [],
    }


def _response_text(output: AgentLoopOutput) -> str | None:
    if output.decision.action == "answer":
        return output.answer
    if output.decision.action == "ask_user":
        return output.decision.question
    return None


def _grounded_answer_text(answer: GroundedAnswer) -> str:
    lines: list[str] = []
    length = 0
    for claim in answer.claims:
        extra = len(claim.text) + (1 if lines else 0)
        if length + extra > 1200:
            break
        lines.append(claim.text)
        length += extra
    return "\n".join(lines) or "The artifact did not contain evidence rows."


def _as_grounding_evidence(result: object) -> ToolResult | None:
    if not isinstance(result, ToolResult):
        return None
    if not result.ok or not result.artifact or not result.checksum:
        return None
    if result.tool is ToolName.QUERY_RESULT_EVIDENCE:
        return result
    if result.tool is ToolName.QUERY_ARTIFACT:
        return result.model_copy(update={"tool": ToolName.QUERY_RESULT_EVIDENCE})
    return None


def _advance_model_budget(budget: StepBudget, usage: object | None) -> StepBudget:
    prompt_tokens = _reported_token_value(usage, "prompt_tokens")
    completion_tokens = _reported_token_value(usage, "completion_tokens")
    total_tokens = _reported_token_value(usage, "total_tokens")
    usage_unknown = total_tokens is None
    return budget.model_copy(update={
        "used_model_steps": budget.used_model_steps + 1,
        "used_prompt_tokens": budget.used_prompt_tokens + (prompt_tokens or 0),
        "used_completion_tokens": budget.used_completion_tokens + (completion_tokens or 0),
        # model_copy(update=...) skips validation, so keep counters bounded here.
        "used_tokens": min(
            budget.max_tokens,
            budget.used_tokens + (total_tokens or 0),
        ),
        "unknown_usage_model_calls": budget.unknown_usage_model_calls + int(usage_unknown),
    })


def _reported_token_value(usage: object | None, field: str) -> int | None:
    if getattr(usage, "status", None) != "reported":
        return None
    value = getattr(usage, field, None)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _advance_tool_budget(budget: StepBudget) -> StepBudget:
    return budget.model_copy(update={
        "used_tool_calls": budget.used_tool_calls + 1,
    })


def _serialize_tool_result(result: object) -> str:
    if hasattr(result, "model_dump"):
        payload = result.model_dump(mode="json")
    else:
        payload = deepcopy(result)
    text = _json_compact(payload)
    if len(text) <= _TOOL_SUMMARY_MAX_CHARS:
        return text or "{}"

    bounded = deepcopy(payload)
    _truncate_payload_lists(bounded)
    text = _json_compact(bounded)
    if len(text) <= _TOOL_SUMMARY_MAX_CHARS:
        return text or "{}"

    LOG.error(
        "tool result exceeded the serialized summary limit after structural truncation",
        extra={
            "event": "agent.tool.summary_too_large",
            "summary_chars": len(text),
            "summary_limit": _TOOL_SUMMARY_MAX_CHARS,
        },
    )
    return _json_compact({
        "ok": False,
        "truncated": True,
        "error_code": "tool_result_summary_too_large",
    })


def _json_compact(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)


def _truncate_payload_lists(payload: object) -> bool:
    """Shrink list-heavy result payloads before serialization, preserving JSON."""

    if not isinstance(payload, (dict, list)):
        return False
    changed = False
    while len(_json_compact(payload)) > _TOOL_SUMMARY_MAX_CHARS:
        targets = _list_targets(payload)
        if not targets:
            break
        values, owner = max(
            targets,
            key=lambda item: (len(item[0]), len(_json_compact(item[0]))),
        )
        new_length = max(0, len(values) // 2)
        if new_length == len(values):
            new_length -= 1
        del values[new_length:]
        changed = True
        if isinstance(owner, dict):
            if isinstance(owner.get("truncated"), bool):
                owner["truncated"] = True
            elif isinstance(payload, dict):
                payload["truncated"] = True
    return changed


def _list_targets(node: object, owner: dict[str, object] | None = None) -> list[
    tuple[list[object], dict[str, object] | None]
]:
    targets: list[tuple[list[object], dict[str, object] | None]] = []
    if isinstance(node, dict):
        for value in node.values():
            if isinstance(value, list) and value:
                targets.append((value, node))
                targets.extend(_list_targets(value, node))
            elif isinstance(value, dict):
                targets.extend(_list_targets(value, node))
    elif isinstance(node, list):
        for value in node:
            if isinstance(value, (dict, list)):
                targets.extend(_list_targets(value, owner))
    return targets


def _budget_question(observations: list[ToolObservation]) -> str:
    if observations:
        return (
            f"I checked {len(observations)} data source(s), but the request reached its "
            "execution budget. Please confirm the remaining operation or narrow the request."
        )
    return _STEP_BUDGET_QUESTION


def _list_jobs_response(summary: str) -> str:
    try:
        payload = json.loads(summary)
    except (TypeError, ValueError):
        return "I could not read the available job list."
    if not isinstance(payload, dict):
        return "I could not read the available job list."
    rows = payload.get("rows") or payload.get("jobs") or []
    if not isinstance(rows, list) or not rows:
        return "No available jobs."
    items: list[str] = []
    for row in rows[:20]:
        if not isinstance(row, dict):
            continue
        job_id = str(row.get("job_id") or "").strip()
        status = str(row.get("status") or "unknown").strip()
        if job_id:
            items.append(f"{job_id} ({status})")
    return "Available jobs: " + ", ".join(items) if items else "No available jobs."


# TODO(P0-3): Replace schema-coupled Python summaries with model-generated
# responses from the bounded tool summary, as the repetition path already does.
def _read_only_observation_response(tool: ToolName, summary: str) -> str:
    try:
        payload = json.loads(summary)
    except (TypeError, ValueError):
        return "I could not read the requested metadata."
    if not isinstance(payload, dict):
        return "I could not read the requested metadata."
    rows = payload.get("rows") or payload.get("fields") or payload.get("candidates") or []
    if tool is ToolName.DESCRIBE_METADATA:
        fields = [
            row.get("field")
            for row in rows
            if isinstance(row, dict) and row.get("field")
        ] if isinstance(rows, list) else []
        if isinstance(fields, list) and fields:
            return "Metadata fields: " + ", ".join(str(item) for item in fields)
        return "No metadata fields were available."
    if isinstance(rows, list) and rows:
        parts: list[str] = []
        for row in rows[:20]:
            if not isinstance(row, dict):
                continue
            field = row.get("compare_field")
            tested = row.get("tested_level")
            reference = row.get("reference_level")
            if field and tested and reference:
                parts.append(f"{tested} versus {reference} in {field}")
        if parts:
            return "Valid contrasts: " + "; ".join(parts)
    return "No valid contrasts were available."


def _should_retry_followup(context: AgentControlContext) -> bool:
    """Identify an explicit rewrite/correction of an existing answer."""

    if not any(message.role == "assistant" for message in context.recent_messages.messages):
        return False
    text = context.user_message.casefold().strip()
    markers = (
        "concise",
        "shorter",
        "plain language",
        "correct that",
        "clarify that",
        "use plain",
        "instead",
        "mention ",
        "caveat",
        "mitigation",
        "paired groups",
        "\u6539\u6b63",
        "\u7b80\u77ed",
        "\u901a\u4fd7",
    )
    return any(marker in text for marker in markers)


def _should_force_list_jobs(
    context: AgentControlContext,
    decision: AgentDecision,
    tool_executor: ToolExecutor | None,
) -> bool:
    """Require the read-only jobs tool for an explicit list-jobs request."""

    if tool_executor is None:
        return False
    if decision.action == "tool_call" and decision.tool is ToolName.LIST_JOBS:
        return False
    return _router_is_explicit_jobs_listing(context.user_message)
