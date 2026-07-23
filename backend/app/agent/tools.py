from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from ..models import AnalysisType
from .schemas import ToolName, ToolResult


def inspect_uploaded_inputs() -> ToolResult:
    raise NotImplementedError("inspect_uploaded_inputs 将在 Phase 3 实现")


def get_analysis_spec(analysis_type: AnalysisType | str) -> ToolResult:
    raise NotImplementedError("get_analysis_spec 将在 Phase 3 实现")


def run_preflight(analysis_type: AnalysisType | str, params: Mapping[str, Any]) -> ToolResult:
    raise NotImplementedError("run_preflight 将在 Phase 3 实现")


def submit_approved_plan(plan_id: str, idempotency_key: str) -> ToolResult:
    raise NotImplementedError("submit_approved_plan 将在 Phase 3 实现")


def get_jobs_status(job_ids: Sequence[str]) -> ToolResult:
    raise NotImplementedError("get_jobs_status 将在 Phase 3 实现")


def query_result_evidence(
    job_id: str,
    artifact: str,
    filters: Mapping[str, Any] | None = None,
    sort: str | None = None,
    limit: int | None = None,
    resolve_entity: str | None = None,
) -> ToolResult:
    raise NotImplementedError("query_result_evidence 将在 Phase 3 实现")


ToolHandler = Callable[..., ToolResult]


class ToolExecutor(Protocol):
    """工具执行接口；超时、错误归类与裁剪将在后续 Phase 实现。"""

    def execute(self, name: ToolName, **kwargs: Any) -> ToolResult:
        ...


class ToolRegistry:
    """六个固定工具名的注册表；当前全部显式未实现。"""

    def __init__(self) -> None:
        self._handlers: dict[ToolName, ToolHandler] = {
            ToolName.INSPECT_UPLOADED_INPUTS: inspect_uploaded_inputs,
            ToolName.GET_ANALYSIS_SPEC: get_analysis_spec,
            ToolName.RUN_PREFLIGHT: run_preflight,
            ToolName.SUBMIT_APPROVED_PLAN: submit_approved_plan,
            ToolName.GET_JOBS_STATUS: get_jobs_status,
            ToolName.QUERY_RESULT_EVIDENCE: query_result_evidence,
        }

    def names(self) -> tuple[ToolName, ...]:
        return tuple(self._handlers)

    def call(self, name: ToolName | str, **kwargs: Any) -> ToolResult:
        return self._handlers[ToolName(name)](**kwargs)
