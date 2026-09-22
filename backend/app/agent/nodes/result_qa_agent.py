from __future__ import annotations

from collections.abc import Callable

from ..context import ContextAssembler
from ..graph import AgentRole, GraphState, MainDecisionModel, MainModelOutput, ToolExecutor
from ..schemas import ToolName
from ..trace import TraceRecorder
from .main import _run_agent_loop


def result_qa_agent_node(
    model: MainDecisionModel,
    tool_executor: ToolExecutor | None = None,
    trace_recorder: TraceRecorder | None = None,
) -> Callable[[GraphState], dict[str, object]]:
    return _run_agent_loop(
        AgentRole.RESULT_QA,
        model,
        tool_executor,
        trace_recorder,
        ContextAssembler().assemble,
        MainModelOutput,
        {ToolName.LIST_JOBS, ToolName.DESCRIBE_ARTIFACTS, ToolName.QUERY_ARTIFACT},
    )


__all__ = ["result_qa_agent_node"]
