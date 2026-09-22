from __future__ import annotations

from collections.abc import Callable

from ..context import ContextAssembler
from ..graph import AgentRole, AnalysisModelOutput, GraphState, MainDecisionModel, ToolExecutor
from ..schemas import ToolName
from ..trace import TraceRecorder
from .main import _run_agent_loop


def analysis_agent_node(
    model: MainDecisionModel,
    tool_executor: ToolExecutor | None = None,
    trace_recorder: TraceRecorder | None = None,
) -> Callable[[GraphState], dict[str, object]]:
    return _run_agent_loop(
        AgentRole.ANALYSIS,
        model,
        tool_executor,
        trace_recorder,
        ContextAssembler().assemble_for_analysis,
        AnalysisModelOutput,
        {ToolName.DESCRIBE_METADATA, ToolName.ENUMERATE_CONTRASTS},
    )


__all__ = ["analysis_agent_node"]
