from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from pydantic import ValidationError

from ..graph import (
    AgentDecision,
    GraphState,
    MainDecisionModel,
    MainModelOutput,
    StepBudget,
)
from ..context import ContextAssembler, MainModelContext


_MODEL_FALLBACK_QUESTION = (
    "我暂时无法可靠判断你的意图。请说明你是想了解一般知识、运行分析，"
    "还是查询已有任务或结果。"
)
_STEP_BUDGET_QUESTION = "当前请求已达到执行步数上限，请重新描述你要完成的操作。"


def main_node(model: MainDecisionModel) -> Callable[[GraphState], dict[str, object]]:
    def run(state: GraphState) -> dict[str, object]:
        budget = state.step_budget
        if budget.used_steps >= budget.max_steps:
            return _ask_user_update(_STEP_BUDGET_QUESTION, budget)

        next_budget = budget.model_copy(update={"used_steps": budget.used_steps + 1})
        context = _main_context(state)
        for _attempt in range(2):
            try:
                output = MainModelOutput.model_validate(model(context))
            except (Exception, ValidationError):
                continue
            response_text = (
                output.answer
                if output.decision.action == "answer"
                else output.decision.question
                if output.decision.action == "ask_user"
                else None
            )
            return {
                "decision": output.decision,
                "response_text": response_text,
                "step_budget": next_budget,
            }
        return _ask_user_update(_MODEL_FALLBACK_QUESTION, next_budget)

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
) -> dict[str, object]:
    return {
        "decision": AgentDecision(
            action="ask_user",
            question=question,
            decision_note="deterministic model fallback",
        ),
        "response_text": question,
        "step_budget": budget,
    }
