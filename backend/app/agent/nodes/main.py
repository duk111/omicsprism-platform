from __future__ import annotations

import json
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
from ..schemas import GroundedAnswer, ToolName, ToolResult


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
                    "step_budget": budget,
                    "tool_observations": observations,
                }
            if decision.action != "tool_call":
                return {
                    "decision": decision,
                    "response_text": _response_text(output),
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
            try:
                result = tool_executor(request, working_state)
                summary = _serialize_tool_result(result)
                evidence = _as_grounding_evidence(result)
                if evidence is not None:
                    latest_evidence = evidence
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
        "grounded_answer": None,
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
