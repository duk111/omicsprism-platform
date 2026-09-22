from __future__ import annotations

from backend.app.agent.graph import AgentRole, GraphState, JobRef, PendingAnalysisClarification
from backend.app.agent.router import route


def _state(message: str, *, pending: bool = True, jobs: bool = True) -> GraphState:
    return GraphState(
        thread_id="thread-router",
        user_id="user-router",
        user_message=message,
        pending_analysis=(
            PendingAnalysisClarification(
                status="active",
                question="Which contrast?",
                missing=["contrast"],
                options=["control vs salt", "salt vs control"],
                source_message="Compare treatment groups",
                input_bundle_id="bundle-1",
            )
            if pending
            else None
        ),
        recent_jobs=([JobRef(job_id="job-old", owner_id="user-router")] if jobs else []),
    )


def test_pending_short_clarification_stays_with_analysis() -> None:
    assert route(_state("\u7b2c\u4e8c\u79cd")) is AgentRole.ANALYSIS


def test_pending_other_question_with_do_not_words_goes_to_qa() -> None:
    assert route(_state("\u4e0d\u8981\u53ea\u770b\u7ed3\u679c\uff0c\u4ec0\u4e48\u662f FDR\uff1f")) is AgentRole.QA
    assert route(_state("\u4e0d\u8981\u53ea\u770b\u7ed3\u679c\uff0c\u7ee7\u7eed\u5206\u6790")) is AgentRole.ANALYSIS


def test_pending_knowledge_question_then_continue_routes_correctly() -> None:
    assert route(_state("What is FDR?")) is AgentRole.QA
    assert route(_state("\u7ee7\u7eed\u521a\u624d\u5206\u6790")) is AgentRole.ANALYSIS


def test_reupload_routes_to_analysis_and_does_not_use_old_job() -> None:
    assert route(_state("\u91cd\u65b0\u4e0a\u4f20\u6570\u636e")) is AgentRole.ANALYSIS


def test_explicit_job_request_requires_existing_job_context() -> None:
    assert route(_state("show job status", pending=False, jobs=True)) is AgentRole.RESULT_QA
    assert route(_state("show job status", pending=False, jobs=False)) is AgentRole.QA


def test_explicit_result_query_with_pending_analysis_stays_result_qa() -> None:
    assert route(_state("Show the fold change result for GeneA")) is AgentRole.RESULT_QA
