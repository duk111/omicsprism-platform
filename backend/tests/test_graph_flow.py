from __future__ import annotations

from backend.app.agent.graph import (
    AgentDecision,
    GraphState,
    MainModelContext,
    MainModelOutput,
    StepBudget,
    build_agent_graph,
)


class ScriptedMainModel:
    def __init__(self, outputs: list[object]) -> None:
        self.outputs = list(outputs)
        self.contexts: list[MainModelContext] = []

    def __call__(self, context: MainModelContext) -> object:
        self.contexts.append(context)
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return output


def _state(**overrides: object) -> GraphState:
    values: dict[str, object] = {
        "thread_id": "thread-1",
        "user_id": "user-1",
        "user_message": "What is differential expression?",
    }
    values.update(overrides)
    return GraphState.model_validate(values)


def _run(model: ScriptedMainModel, state: GraphState | None = None) -> GraphState:
    result = build_agent_graph(model).invoke(state or _state())
    return GraphState.model_validate(result)


def test_general_knowledge_routes_to_direct_answer() -> None:
    model = ScriptedMainModel([MainModelOutput(
        decision=AgentDecision(action="answer", decision_note="general knowledge"),
        answer="Differential expression compares feature abundance between conditions.",
    )])

    result = _run(model)

    assert result.decision is not None
    assert result.decision.action == "answer"
    assert result.response_text.startswith("Differential expression")
    assert result.step_budget.used_steps == 1


def test_insufficient_intent_routes_to_ask_user() -> None:
    model = ScriptedMainModel([{
        "decision": {"action": "ask_user", "question": "What would you like to do?"},
        "answer": None,
    }])

    result = _run(model, _state(user_message="Help"))

    assert result.decision is not None
    assert result.decision.action == "ask_user"
    assert result.response_text == "What would you like to do?"


def test_schema_failure_retries_once_then_asks_user() -> None:
    model = ScriptedMainModel([
        {"decision": {"action": "not-valid"}},
        {"decision": {"action": "still-invalid"}},
    ])

    result = _run(model)

    assert len(model.contexts) == 2
    assert result.decision is not None
    assert result.decision.action == "ask_user"
    assert "无法可靠判断" in (result.response_text or "")


def test_model_error_can_recover_on_the_single_retry() -> None:
    model = ScriptedMainModel([
        RuntimeError("model unavailable"),
        MainModelOutput(
            decision=AgentDecision(action="answer"),
            answer="Recovered answer.",
        ),
    ])

    result = _run(model)

    assert len(model.contexts) == 2
    assert result.response_text == "Recovered answer."


def test_specialist_routes_are_placeholders_without_side_effects() -> None:
    for action in ("run_analysis", "query_result"):
        model = ScriptedMainModel([MainModelOutput(
            decision=AgentDecision(action=action),
        )])

        result = _run(model)

        assert result.decision is not None
        assert result.decision.action == action
        assert result.response_text is None
        assert result.current_job is None


def test_graph_has_only_three_semantic_nodes() -> None:
    graph = build_agent_graph(ScriptedMainModel([])).get_graph()

    assert set(graph.nodes) == {"__start__", "main", "analysis", "result_qa", "__end__"}


def test_main_model_context_excludes_owner_and_dataset_payloads() -> None:
    model = ScriptedMainModel([MainModelOutput(
        decision=AgentDecision(action="answer"),
        answer="A bounded answer.",
    )])

    _run(model)

    payload = model.contexts[0].model_dump()
    assert set(payload) == {
        "user_message",
        "conversation_summary",
        "dataset_roles",
        "current_job_id",
        "recent_job_ids",
    }
    assert "user_id" not in payload
    assert "owner_id" not in payload


def test_exhausted_step_budget_does_not_call_model() -> None:
    model = ScriptedMainModel([])

    result = _run(model, _state(step_budget=StepBudget(max_steps=1, used_steps=1)))

    assert model.contexts == []
    assert result.decision is not None
    assert result.decision.action == "ask_user"
    assert result.step_budget.used_steps == 1
