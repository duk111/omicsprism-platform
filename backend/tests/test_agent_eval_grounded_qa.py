from __future__ import annotations

from backend.app.agent.domain_eval import (
    evaluate_grounded_qa_case,
    evaluate_grounded_qa_cases,
    load_grounded_qa_cases,
)


def test_grounded_qa_cases_cover_supported_hallucinated_and_insufficient_evidence() -> None:
    cases = load_grounded_qa_cases()

    assert len(cases) == 3
    assert {case.expected_evidence_sufficient for case in cases} == {True, False}


def test_grounded_qa_metrics_reject_unsupported_entity_and_refuse_empty_evidence() -> None:
    evaluation = evaluate_grounded_qa_cases(load_grounded_qa_cases())
    results = {result.case_id: result for result in evaluation.cases}

    assert evaluation.case_count == 3
    assert evaluation.citation_validity == 2 / 3
    assert evaluation.numeric_consistency == 2 / 3
    assert evaluation.hallucinated_entity_count == 1
    assert evaluation.unsupported_claim_rate == 1 / 3
    assert results["grounded_numeric_entity_001"].matched
    assert not results["grounded_hallucinated_entity_001"].matched
    assert results["grounded_hallucinated_entity_001"].hallucinated_entity_count == 1
    assert results["grounded_insufficient_evidence_001"].refused_insufficient_evidence
    assert results["grounded_insufficient_evidence_001"].matched


def test_grounded_qa_rejects_artifact_outside_job() -> None:
    case = load_grounded_qa_cases()[0].model_copy(
        update={"job": load_grounded_qa_cases()[0].job.model_copy(update={"artifacts": []})}
    )

    result = evaluate_grounded_qa_case(case)

    assert not result.matched
    assert "artifact" in result.issues[0]


def test_grounded_qa_rejects_wrong_checksum_and_row_id() -> None:
    case = load_grounded_qa_cases()[0]
    assert case.draft is not None
    for citation in (
        case.draft.claims[0].citation.model_copy(update={"checksum": "sha256:wrong"}),
        case.draft.claims[0].citation.model_copy(update={"row_ids": [99]}),
    ):
        draft = case.draft.model_copy(update={
            "claims": [case.draft.claims[0].model_copy(update={"citation": citation})],
        })
        result = evaluate_grounded_qa_case(case.model_copy(update={"draft": draft}))

        assert not result.matched
        assert not result.citation_valid
