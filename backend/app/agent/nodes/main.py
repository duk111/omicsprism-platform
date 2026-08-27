from __future__ import annotations

import json
from collections.abc import Callable
from typing import Literal

from pydantic import ValidationError

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


_MODEL_FALLBACK_QUESTION = (
    "我暂时无法可靠判断你的意图。请说明你是想了解一般知识、运行分析，"
    "还是查询已有任务或结果。"
)
_STEP_BUDGET_QUESTION = "当前请求已达到执行步数上限，请重新描述你要完成的操作。"


def main_node(
    model: MainDecisionModel,
    tool_executor: ToolExecutor | None = None,
) -> Callable[[GraphState], dict[str, object]]:
    def run(state: GraphState) -> dict[str, object]:
        budget = state.step_budget
        observations = list(state.tool_observations)
        working_state = state
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
                    candidate = MainModelOutput.model_validate(model(context))
                except (Exception, ValidationError):
                    budget = _advance_model_budget(budget, 0)
                    continue
                budget = _advance_model_budget(budget, _estimate_tokens(candidate))
                output = candidate
                break
            if output is None:
                return _ask_user_update(_MODEL_FALLBACK_QUESTION, budget, observations)

            decision = output.decision
            if decision.action != "tool_call":
                return {
                    "decision": decision,
                    "response_text": _response_text(output),
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
            try:
                result = tool_executor(request, working_state)
                summary = _serialize_tool_result(result)
            except Exception:
                summary = "tool execution failed"
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
    if state.decision.action in {"inspect_dataset", "run_analysis"}:
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
        "response_text": question,
        "step_budget": budget,
        "tool_observations": observations or [],
    }


def _response_text(output: MainModelOutput) -> str | None:
    if output.decision.action == "answer":
        return output.answer
    if output.decision.action == "ask_user":
        return output.decision.question
    return None


def _advance_model_budget(budget: StepBudget, token_count: int) -> StepBudget:
    return budget.model_copy(update={
        "used_model_steps": budget.used_model_steps + 1,
        # model_copy(update=...) skips validation, so keep counters bounded here.
        "used_tokens": min(
            budget.max_tokens,
            budget.used_tokens + max(0, token_count),
        ),
    })


def _advance_tool_budget(budget: StepBudget) -> StepBudget:
    return budget.model_copy(update={
        "used_tool_calls": budget.used_tool_calls + 1,
    })


def _estimate_tokens(output: MainModelOutput) -> int:
    serialized = output.model_dump_json()
    return max(1, (len(serialized) + 3) // 4)


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
