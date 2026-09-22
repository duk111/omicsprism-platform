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
    GraphState,
    MainDecisionModel,
    MainModelOutput,
    StepBudget,
    ToolCallRequest,
    ToolExecutor,
    ToolObservation,
)
from ..context import ContextAssembler, MainModelContext
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


def main_node(
    model: MainDecisionModel,
    tool_executor: ToolExecutor | None = None,
    trace_recorder: TraceRecorder | None = None,
) -> Callable[[GraphState], dict[str, object]]:
    def run(state: GraphState) -> dict[str, object]:
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
            context = _main_context(working_state)
            if repetition_guidance is not None:
                context = context.model_copy(update={
                    "tool_repetition_guidance": repetition_guidance,
                })
            output = None
            for _attempt in range(2):
                if (
                    budget.used_model_steps >= budget.max_model_steps
                    or budget.used_tokens >= budget.max_tokens
                ):
                    break
                try:
                    attempt_context = context
                    if _attempt:
                        summary = context.conversation_summary or ""
                        retry_instruction = (
                            _FOLLOWUP_RETRY_INSTRUCTION
                            if _should_retry_followup(context)
                            else _MODEL_RETRY_INSTRUCTION
                        )
                        attempt_context = context.model_copy(update={
                            "conversation_summary": (
                                f"{summary}\n{retry_instruction}"
                                if summary else retry_instruction
                            )[:1200],
                        })
                    candidate = MainModelOutput.model_validate(model(attempt_context))
                except (Exception, ValidationError) as exc:
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
                    return _ask_user_update(_MODEL_FALLBACK_QUESTION, budget, observations)
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
                return _ask_user_update(_MODEL_FALLBACK_QUESTION, budget, observations)

            decision = output.decision
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
                if decision.tool is ToolName.LIST_JOBS and _is_explicit_jobs_listing(context.user_message):
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
            if decision.action != "tool_call":
                response_text = _response_text(output)
                return {
                    "decision": decision,
                    "response_text": response_text,
                    "response_blocks": (
                        [text_block(response_text)] if response_text is not None else []
                    ),
                    "grounded_answer": decision.grounded_answer,
                    "step_budget": budget,
                    "tool_observations": observations,
                }
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
                retry_count=0,
            ))
            observations = observations[-12:]
            budget = _advance_tool_budget(budget)
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

    return run


def _run_agent_loop(
    role: object,
    model: MainDecisionModel,
    tool_executor: ToolExecutor | None,
    trace_recorder: TraceRecorder | None,
    context_builder: Callable[[GraphState], object],
    output_model: type[BaseModel],
    allowed_tools: set[ToolName],
) -> Callable[[GraphState], dict[str, object]]:
    """Shared bounded loop factory used by the role-specific graph nodes.

    The current compatibility loop remains centralized in ``main_node`` while
    role nodes provide their context/schema/tool contract at this boundary.
    Subsequent protocol tightening can replace the legacy adapter here without
    duplicating budget, retry, and transcript handling.
    """

    del context_builder, output_model, allowed_tools
    legacy = main_node(model, tool_executor, trace_recorder)

    def run(state: GraphState) -> dict[str, object]:
        result = legacy(state)
        decision = result.get("decision")
        if isinstance(decision, AgentDecision) and decision.action == "reroute":
            next_count = state.reroute_count + 1
            if next_count > 2:
                return _ask_user_update(_MODEL_FALLBACK_QUESTION, state.step_budget)
            result["reroute_count"] = next_count
        return result

    return run


def route_node(state: GraphState) -> dict[str, object]:
    """No-op graph node; conditional edges perform the role selection."""

    return {}


def route_after_route(state: GraphState) -> Literal["qa", "analysis", "result_qa"]:
    from ..router import route

    return route(state).value  # type: ignore[return-value]


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


def route_after_main(state: GraphState) -> Literal["analysis", "result_qa", "end"]:
    if state.decision is None:
        return "end"
    if state.decision.action in {"inspect_dataset", "run_analysis", "propose_plan"}:
        return "analysis"
    if state.decision.action in {"get_job", "query_result"}:
        return "result_qa"
    return "end"


def _main_context(state: GraphState) -> MainModelContext:
    return ContextAssembler().assemble(state)


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
            error_code=tool_error_code,
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
) -> dict[str, object]:
    return {
        "decision": AgentDecision(
            action="ask_user",
            question=question,
            decision_note="deterministic model fallback",
        ),
        "grounded_answer": None,
        "response_text": question,
        "response_blocks": [text_block(question)],
        "step_budget": budget,
        "tool_observations": observations or [],
    }


def _response_text(output: MainModelOutput) -> str | None:
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


def _should_retry_followup(context: MainModelContext) -> bool:
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
    context: MainModelContext,
    decision: AgentDecision,
    tool_executor: ToolExecutor | None,
) -> bool:
    """Require the read-only jobs tool for an explicit list-jobs request."""

    if tool_executor is None:
        return False
    if decision.action == "tool_call" and decision.tool is ToolName.LIST_JOBS:
        return False
    return _router_is_explicit_jobs_listing(context.user_message)
