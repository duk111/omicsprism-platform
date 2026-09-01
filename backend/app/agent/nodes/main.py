from __future__ import annotations

import json
import logging
from time import perf_counter
from collections.abc import Callable
from typing import Literal

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


LOG = logging.getLogger("omicsprism.platform.agent_main")


def main_node(
    model: MainDecisionModel,
    tool_executor: ToolExecutor | None = None,
    trace_recorder: TraceRecorder | None = None,
) -> Callable[[GraphState], dict[str, object]]:
    def run(state: GraphState) -> dict[str, object]:
        budget = state.step_budget
        observations = list(state.tool_observations)
        working_state = state
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
                        attempt_context = context.model_copy(update={
                            "conversation_summary": (
                                f"{summary}\n{_MODEL_RETRY_INSTRUCTION}"
                                if summary else _MODEL_RETRY_INSTRUCTION
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
                output = candidate
                break
            if output is None:
                return _ask_user_update(_MODEL_FALLBACK_QUESTION, budget, observations)

            decision = output.decision
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
            tool_started = perf_counter()
            tool_outcome = "ok"
            tool_error_code: str | None = None
            try:
                result = tool_executor(request, working_state)
                summary = _serialize_tool_result(result)
                evidence = _as_grounding_evidence(result)
                if evidence is not None:
                    latest_evidence = evidence
            except Exception as exc:
                summary = "tool execution failed"
                tool_outcome = "failed"
                tool_error_code = type(exc).__name__
            if trace_recorder is not None:
                trace_recorder.tool_call(
                    context=working_state,
                    tool_name=request.tool.value,
                    tool_schema_hash=stable_hash(ToolCallRequest.model_json_schema()),
                    latency_ms=round((perf_counter() - tool_started) * 1000, 3),
                    outcome=tool_outcome,
                    error_code=tool_error_code,
                )
            observations.append(ToolObservation(tool=request.tool, summary=summary))
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
        payload = result
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str)
    return text[:3900] or "{}"


def _budget_question(observations: list[ToolObservation]) -> str:
    if observations:
        return (
            f"I checked {len(observations)} data source(s), but the request reached its "
            "execution budget. Please confirm the remaining operation or narrow the request."
        )
    return _STEP_BUDGET_QUESTION
