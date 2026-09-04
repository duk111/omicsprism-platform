from __future__ import annotations

from backend.app.agent.graph import (
    AgentDecision,
    GraphState,
    MainModelOutput,
    StepBudget,
    ToolCallRequest,
    build_agent_graph,
)
from backend.app.agent.grounding import FALLBACK_TEXT
from backend.app.agent.context import RecentMessage, RecentMessages
from backend.app.agent.readonly_tools import MetadataDescription
from backend.app.agent.schemas import (
    Citation,
    GroundedAnswer,
    GroundedClaim,
    ToolName,
    ToolResult,
)
from backend.app.agent.trace import ModelUsage


class _Model:
    def __init__(self, outputs: list[object]) -> None:
        self.outputs = list(outputs)
        self.contexts = []

    def __call__(self, context: object) -> object:
        self.contexts.append(context)
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return output


def _state(**overrides: object) -> GraphState:
    values: dict[str, object] = {
        "thread_id": "loop-thread",
        "user_id": "user-1",
        "user_message": "Inspect the metadata",
    }
    values.update(overrides)
    return GraphState.model_validate(values)


def _config() -> dict[str, dict[str, str]]:
    return {"configurable": {"thread_id": "loop-thread"}}


def test_main_loop_executes_read_only_tool_and_reassembles_context() -> None:
    model = _Model([
        MainModelOutput(decision=AgentDecision(
            action="tool_call",
            tool=ToolName.DESCRIBE_METADATA,
            arguments={"fields": ["treatment"]},
        )),
        MainModelOutput(
            decision=AgentDecision(action="answer"),
            answer="The metadata contains the requested treatment field.",
        ),
    ])
    requests: list[ToolCallRequest] = []

    def execute(request: ToolCallRequest, _state: GraphState) -> MetadataDescription:
        requests.append(request)
        return MetadataDescription(
            fields=[], alignment={"counts": "exact"}, sample_count=4
        )

    result = build_agent_graph(
        model,
        lambda _request: [],
        lambda _request: None,
        lambda _request: None,
        lambda _request: None,
        tool_executor=execute,
    ).invoke(_state(), _config())
    state = GraphState.model_validate(result)

    assert state.response_text.startswith("The metadata")
    assert len(model.contexts) == 2
    assert requests[0].tool is ToolName.DESCRIBE_METADATA
    assert requests[0].arguments == {"fields": ["treatment"]}
    assert model.contexts[1].working_set.items[0].kind == "tool"
    assert state.step_budget.used_model_steps == 2
    assert state.step_budget.used_tool_calls == 1


def test_tool_budget_is_an_exit_without_an_extra_model_call() -> None:
    model = _Model([
        MainModelOutput(decision=AgentDecision(
            action="tool_call", tool=ToolName.DESCRIBE_METADATA
        )),
        MainModelOutput(decision=AgentDecision(action="answer"), answer="unexpected"),
    ])
    result = build_agent_graph(
        model,
        lambda _request: [],
        lambda _request: None,
        lambda _request: None,
        lambda _request: None,
        tool_executor=lambda _request, _state: {"fields": []},
    ).invoke(
        _state(step_budget=StepBudget(max_tool_calls=1)),
        _config(),
    )
    state = GraphState.model_validate(result)

    assert len(model.contexts) == 1
    assert state.decision is not None and state.decision.action == "ask_user"
    assert state.step_budget.used_tool_calls == 1
    assert "budget" in (state.response_text or "").lower()


def test_model_failure_retry_consumes_model_steps_and_then_continues() -> None:
    model = _Model([
        {"decision": {"action": "invalid"}},
        MainModelOutput(decision=AgentDecision(action="answer"), answer="recovered"),
    ])
    result = build_agent_graph(
        model,
        lambda _request: [],
        lambda _request: None,
        lambda _request: None,
        lambda _request: None,
    ).invoke(_state(), _config())
    state = GraphState.model_validate(result)

    assert len(model.contexts) == 2
    assert state.response_text == "recovered"
    assert state.step_budget.used_model_steps == 2


def test_explicit_followup_retries_an_initial_clarification() -> None:
    model = _Model([
        MainModelOutput(decision=AgentDecision(
            action="ask_user", question="Which dataset do you mean?"
        )),
        MainModelOutput(
            decision=AgentDecision(action="answer"),
            answer="It compares abundance between groups.",
        ),
    ])
    result = build_agent_graph(
        model,
        lambda _request: [],
        lambda _request: None,
        lambda _request: None,
        lambda _request: None,
    ).invoke(
        _state(
            user_message="Use a concise explanation instead.",
            recent_messages=RecentMessages(
                context_version="messages.v1:test",
                messages=[RecentMessage(
                    role="assistant", turn_id="previous", text="Differential expression compares feature abundance."
                )],
            ),
        ),
        _config(),
    )
    state = GraphState.model_validate(result)

    assert state.decision is not None and state.decision.action == "answer"
    assert state.response_text == "It compares abundance between groups."
    assert len(model.contexts) == 2
    assert "revise the previous assistant answer" in (model.contexts[1].conversation_summary or "")


def test_genuine_clarification_with_history_is_not_retried() -> None:
    model = _Model([
        MainModelOutput(decision=AgentDecision(
            action="ask_user", question="Which dataset do you mean?"
        )),
    ])
    result = build_agent_graph(
        model,
        lambda _request: [],
        lambda _request: None,
        lambda _request: None,
        lambda _request: None,
    ).invoke(
        _state(
            user_message="Which dataset should I use?",
            recent_messages=RecentMessages(
                context_version="messages.v1:test",
                messages=[RecentMessage(
                    role="assistant", turn_id="previous", text="I can help with an analysis."
                )],
            ),
        ),
        _config(),
    )
    state = GraphState.model_validate(result)

    assert state.decision is not None and state.decision.action == "ask_user"
    assert len(model.contexts) == 1


def test_list_jobs_request_forces_read_only_tool_before_answer() -> None:
    model = _Model([
        MainModelOutput(
            decision=AgentDecision(action="answer"),
            answer="There are no available jobs.",
        ),
        MainModelOutput(
            decision=AgentDecision(action="answer"),
            answer="One job is available.",
        ),
    ])
    requests: list[ToolCallRequest] = []

    def execute(request: ToolCallRequest, _state: GraphState) -> dict[str, object]:
        requests.append(request)
        return {"jobs": [{"job_id": "job-list", "status": "succeeded"}]}

    result = build_agent_graph(
        model,
        lambda _request: [],
        lambda _request: None,
        lambda _request: None,
        lambda _request: None,
        tool_executor=execute,
    ).invoke(_state(user_message="List available jobs."), _config())
    state = GraphState.model_validate(result)

    assert requests and requests[0].tool.value == "list_jobs"
    assert state.response_text == "One job is available."
    assert len(model.contexts) == 2


def test_step_budget_uses_separate_dimensions() -> None:
    budget = StepBudget(
        max_model_steps=3,
        max_tool_calls=4,
        max_tokens=50,
        used_model_steps=2,
        used_tool_calls=1,
        used_tokens=20,
    )

    assert budget.max_model_steps == 3
    assert budget.max_tool_calls == 4
    assert budget.max_tokens == 50
    assert budget.used_model_steps == 2
    assert budget.used_tool_calls == 1
    assert budget.used_tokens == 20


def test_unknown_model_usage_does_not_fake_a_token_count() -> None:
    model = _Model([
        MainModelOutput(decision=AgentDecision(action="answer"), answer="bounded"),
    ])
    result = build_agent_graph(
        model,
        lambda _request: [],
        lambda _request: None,
        lambda _request: None,
        lambda _request: None,
    ).invoke(
        _state(step_budget=StepBudget(max_tokens=1)),
        _config(),
    )
    state = GraphState.model_validate(result)

    assert state.response_text == "bounded"
    assert state.step_budget.used_tokens == 0
    assert state.step_budget.unknown_usage_model_calls == 1


def test_reported_model_usage_updates_separate_budget_counters() -> None:
    model = _Model([
        MainModelOutput(decision=AgentDecision(action="answer"), answer="bounded"),
    ])
    model.last_usage = ModelUsage(
        status="reported",
        prompt_tokens=8,
        completion_tokens=3,
        total_tokens=11,
    )
    result = build_agent_graph(
        model,
        lambda _request: [],
        lambda _request: None,
        lambda _request: None,
        lambda _request: None,
    ).invoke(_state(), _config())
    state = GraphState.model_validate(result)

    assert state.step_budget.used_prompt_tokens == 8
    assert state.step_budget.used_completion_tokens == 3
    assert state.step_budget.used_tokens == 11
    assert state.step_budget.unknown_usage_model_calls == 0


def test_grounded_loop_verifies_model_draft_and_falls_back_to_evidence() -> None:
    evidence = ToolResult(
        tool=ToolName.QUERY_ARTIFACT,
        ok=True,
        rows=[{"_row_id": 7, "Gene": "GeneA", "padj": "0.01"}],
        truncated=False,
        row_count=1,
        artifact="deg_results.csv",
        checksum="sha256:evidence",
        filters={},
        sort=None,
        error_code=None,
    )
    draft = GroundedAnswer(claims=[GroundedClaim(
        text="GeneA has padj 0.99",
        citation=Citation(
            artifact="deg_results.csv",
            checksum="sha256:evidence",
            row_ids=[99],
        ),
    )])
    model = _Model([
        MainModelOutput(decision=AgentDecision(
            action="tool_call",
            tool=ToolName.QUERY_ARTIFACT,
            arguments={"job_id": "job-1", "artifact": "deg_results.csv"},
        )),
        MainModelOutput(decision=AgentDecision(
            action="grounded_answer",
            grounded_answer=draft,
        )),
    ])

    def execute(_request: ToolCallRequest, _state: GraphState) -> ToolResult:
        return evidence

    result = build_agent_graph(
        model,
        lambda _request: [],
        lambda _request: None,
        lambda _request: None,
        lambda _request: None,
        tool_executor=execute,
    ).invoke(_state(), _config())
    state = GraphState.model_validate(result)

    assert state.grounded_answer is not None
    assert state.grounded_answer.claims[0].text == FALLBACK_TEXT
    assert state.grounded_answer.claims[1].citation.row_ids == [7]
