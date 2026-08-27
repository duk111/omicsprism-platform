from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from ..graph import (
    GraphState,
    JobLookupRequest,
    JobReader,
    JobRef,
    JobSummary,
    NodeCapabilityError,
    ResultEvidenceRequest,
    ResultQuerier,
)
from ..grounding import GroundedAnswerPipeline
from ..schemas import GroundedAnswer, ToolName, ToolResult

if TYPE_CHECKING:
    from ..tools import AgentToolRuntime


class ResultAccessError(ValueError):
    """A read-only result dependency violated its ownership contract."""


def result_qa_node(
    job_reader: JobReader,
    result_querier: ResultQuerier,
) -> Callable[[GraphState], dict[str, object]]:
    """Answer Job and result questions through ownership-bound read capabilities."""

    pipeline = GroundedAnswerPipeline()

    def run(state: GraphState) -> dict[str, object]:
        decision = state.decision
        if decision is None or decision.action not in {"get_job", "query_result"}:
            action = decision.action if decision is not None else "missing"
            raise NodeCapabilityError(
                f"Result QA node does not allow action: {action}"
            )

        if state.step_budget.used_model_steps >= state.step_budget.max_model_steps:
            return {
                "response_text": (
                    "The result question was not processed because the step budget "
                    "was exhausted."
                ),
            }

        budget = state.step_budget.model_copy(
            update={"used_model_steps": state.step_budget.used_model_steps + 1}
        )
        job_ref = _resolve_job_ref(state)
        if job_ref is None:
            return {
                "response_text": _job_selection_question(state),
                "step_budget": budget,
            }

        try:
            summary = job_reader(JobLookupRequest(
                user_id=state.user_id,
                job_id=job_ref.job_id,
            ))
        except LookupError:
            return {
                "response_text": "The selected Job was not found or is not accessible.",
                "step_budget": budget,
            }
        _validate_job_summary(state, job_ref, summary)

        base_update: dict[str, object] = {
            "current_job": JobRef(job_id=summary.job_id, owner_id=summary.owner_id),
            "job_summary": summary,
            "grounded_answer": None,
            "step_budget": budget,
        }
        if decision.action == "get_job":
            return {**base_update, "response_text": _job_summary_text(summary)}
        if decision.result_query is None:
            raise ResultAccessError("query_result action is missing its typed query")

        query = decision.result_query
        if query.artifact not in summary.artifacts:
            return {
                **base_update,
                "response_text": (
                    f"Artifact {query.artifact} is not available for Job {summary.job_id}."
                ),
            }
        try:
            evidence = result_querier(ResultEvidenceRequest(
                user_id=state.user_id,
                job_id=summary.job_id,
                query=query,
            ))
        except LookupError:
            return {
                **base_update,
                "response_text": "The requested result evidence was not found or is not accessible.",
            }
        if not _is_groundable_evidence(evidence, summary):
            return {
                **base_update,
                "response_text": "The requested artifact did not provide verifiable evidence.",
            }

        answer = pipeline.answer(
            evidence,
            draft=state.grounded_answer,
            repair=None,
        )
        return {
            **base_update,
            "grounded_answer": answer,
            "response_text": _answer_text(answer),
        }

    return run


def job_reader_from_runtime(runtime: AgentToolRuntime) -> JobReader:
    """Adapt the existing ownership-bound status tool to the graph contract."""

    def read(request: JobLookupRequest) -> JobSummary:
        _validate_runtime_user(runtime, request.user_id)
        result = runtime.get_job(request.job_id)
        if result.tool is not ToolName.GET_JOBS_STATUS or not result.ok or len(result.rows) != 1:
            raise LookupError(request.job_id)
        row = result.rows[0]
        if str(row.get("job_id", "")) != request.job_id:
            raise ResultAccessError("status tool returned a different Job")
        return JobSummary(
            job_id=request.job_id,
            owner_id=request.user_id,
            status=str(row.get("status") or "unknown"),
            progress=row.get("progress"),
            progress_step=row.get("progress_step"),
            error=row.get("error"),
            artifacts=row.get("artifacts") or [],
        )

    return read


def result_querier_from_runtime(runtime: AgentToolRuntime) -> ResultQuerier:
    """Adapt the existing ownership-bound artifact query to the graph contract."""

    def query(request: ResultEvidenceRequest) -> ToolResult:
        _validate_runtime_user(runtime, request.user_id)
        spec = request.query
        return runtime.query_result(
            request.job_id,
            spec.artifact,
            filters=spec.filters,
            field_path=spec.field_path,
            sort=spec.sort,
            limit=spec.limit,
            resolve_entity=spec.resolve_entity,
        )

    return query


def _resolve_job_ref(state: GraphState) -> JobRef | None:
    decision = state.decision
    if decision is not None and decision.job_id:
        return JobRef(job_id=decision.job_id, owner_id=state.user_id)
    if state.current_job is not None:
        return state.current_job
    unique = {item.job_id: item for item in state.recent_jobs}
    if len(unique) == 1:
        return next(iter(unique.values()))
    return None


def _job_selection_question(state: GraphState) -> str:
    unique_ids = list(dict.fromkeys(item.job_id for item in state.recent_jobs))
    if len(unique_ids) > 1:
        return "Specify which Job to use: " + ", ".join(unique_ids)
    return "Specify the Job whose status or results you want to inspect."


def _validate_job_summary(
    state: GraphState,
    requested: JobRef,
    summary: JobSummary,
) -> None:
    if summary.owner_id != state.user_id:
        raise ResultAccessError("job reader returned a cross-user Job")
    if summary.job_id != requested.job_id:
        raise ResultAccessError("job reader returned a different Job")


def _validate_runtime_user(runtime: AgentToolRuntime, user_id: str) -> None:
    if runtime.user_id != user_id:
        raise ResultAccessError("result runtime does not belong to the graph user")


def _is_groundable_evidence(evidence: ToolResult, summary: JobSummary) -> bool:
    return (
        evidence.tool is ToolName.QUERY_RESULT_EVIDENCE
        and evidence.ok
        and evidence.artifact in summary.artifacts
        and evidence.checksum is not None
    )


def _job_summary_text(summary: JobSummary) -> str:
    text = f"Job {summary.job_id}: {summary.status}"
    if summary.progress is not None:
        text += f" ({summary.progress}%)"
    if summary.progress_step:
        text += f" - {summary.progress_step}"
    if summary.error:
        text += f". Error: {summary.error}"
    if summary.artifacts:
        text += ". Artifacts: " + ", ".join(summary.artifacts)
    return text[:1200]


def _answer_text(answer: GroundedAnswer) -> str:
    lines: list[str] = []
    length = 0
    for claim in answer.claims:
        extra = len(claim.text) + (1 if lines else 0)
        if length + extra > 1200:
            break
        lines.append(claim.text)
        length += extra
    return "\n".join(lines) or "The artifact did not contain evidence rows."
