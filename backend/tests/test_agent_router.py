from __future__ import annotations

from backend.app.agent.clarification_resolver import ClarificationOption, stable_option_id
from backend.app.agent.dataset_profile import MatrixProfile
from backend.app.agent.graph import (
    AgentRole,
    DatasetProfileRef,
    GraphState,
    JobRef,
    PendingAnalysisClarification,
    RouteClassification,
    RouteTarget,
)
from backend.app.agent.nodes.main import route_node
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
                options=[
                    ClarificationOption(
                        option_id=stable_option_id("control vs salt"),
                        label="control vs salt",
                    ),
                    ClarificationOption(
                        option_id=stable_option_id("salt vs control"),
                        label="salt vs control",
                    ),
                ],
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


def test_job_continuation_bypasses_message_rules() -> None:
    state = _state("What is FDR?", pending=False, jobs=False).model_copy(
        update={"turn_origin": "job_continuation"}
    )
    assert route(state) is AgentRole.RESULT_QA


def test_capability_questions_route_to_analysis_without_keyword_analysis_request() -> None:
    assert route(_state("这两个数据能做什么", pending=False, jobs=False)) is AgentRole.ANALYSIS
    assert route(_state("可以做差异分析吗", pending=False, jobs=False)) is AgentRole.ANALYSIS


def test_vague_dataset_request_uses_no_tool_router_classifier() -> None:
    state = _state("帮我看看这些数据", pending=False, jobs=False).model_copy(update={
        "dataset_profiles": [DatasetProfileRef(
            dataset_id="counts-1",
            owner_id="user-router",
            filename="counts.csv",
            checksum="sha256:" + "a" * 64,
            profile=MatrixProfile(
                role="counts",
                shape=(2, 2),
                sample_ids=["s1", "s2"],
                feature_type="gene",
                feature_id_examples=["g1"],
                numeric_type="integer_counts",
                has_negative=False,
                missing_rate=0,
            ),
        )],
    })

    class _Classifier:
        def __call__(self, context, *, role):
            assert role is AgentRole.ROUTER
            assert context.dataset_roles == ["counts"]
            return RouteClassification(target="analysis", confidence=0.9)

    result = route_node(state, _Classifier())

    assert result["route_decision"].target is RouteTarget.ANALYSIS
    assert result["route_decision"].source == "classifier"
    assert result["visited_roles"] == [AgentRole.ANALYSIS]


def test_low_confidence_route_classification_is_terminal_ambiguity() -> None:
    state = _state("帮我看看这些数据", pending=False, jobs=False).model_copy(update={
        "dataset_profiles": [DatasetProfileRef(
            dataset_id="counts-1",
            owner_id="user-router",
            filename="counts.csv",
            checksum="sha256:" + "a" * 64,
            profile=MatrixProfile(
                role="counts",
                shape=(2, 2),
                sample_ids=["s1", "s2"],
                feature_type="gene",
                feature_id_examples=["g1"],
                numeric_type="integer_counts",
                has_negative=False,
                missing_rate=0,
            ),
        )],
    })

    class _Classifier:
        def __call__(self, _context, *, role):
            return RouteClassification(target="analysis", confidence=0.2)

    result = route_node(state, _Classifier())

    assert result["outcome"] == "unresolved"
    assert result["failure_code"] == "route_ambiguous"


def test_explicitly_unsupported_domain_routes_to_unsupported_target() -> None:
    state = _state("请做单细胞分析", pending=False, jobs=False)

    assert route(state) is RouteTarget.UNSUPPORTED
