from __future__ import annotations

import pytest

from backend.app.agent.grounding import (
    NO_EVIDENCE_TEXT,
    EvidenceGrounder,
    EvidenceGroundingError,
    GroundedAnswerPipeline,
)
from backend.app.agent.schemas import (
    Citation,
    GroundedAnswer,
    GroundedClaim,
    ToolName,
    ToolResult,
)
from backend.app.agent.verifier import AnswerVerifier


def _evidence(*, artifact: str = "outputs/T02_High_Confidence_Network.csv", rows=None) -> ToolResult:
    return ToolResult(
        tool=ToolName.QUERY_RESULT_EVIDENCE,
        ok=True,
        rows=rows if rows is not None else [
            {"_row_id": 7, "Source": "GeneA", "Target": "M0123", "EdgeWeight": "0.82", "PearsonR": "0.71"},
        ],
        truncated=False,
        row_count=1,
        artifact=artifact,
        checksum="sha256:evidence",
        filters={},
        sort="EdgeWeight desc",
        error_code=None,
    )


def _answer(text: str, row_ids: list[int] = [7]) -> GroundedAnswer:
    return GroundedAnswer(claims=[GroundedClaim(
        text=text,
        citation=Citation(
            artifact="outputs/T02_High_Confidence_Network.csv",
            checksum="sha256:evidence",
            row_ids=row_ids,
        ),
    )])


def test_empty_evidence_uses_the_fixed_no_evidence_template() -> None:
    answer = EvidenceGrounder().ground(_evidence(rows=[]))

    assert [claim.text for claim in answer.claims] == [NO_EVIDENCE_TEXT]
    assert answer.claims[0].citation.row_ids == []
    assert AnswerVerifier().verify(answer, [_evidence(rows=[])]).verdict.value == "approved"


def test_grounder_assigns_evidence_citations_and_updates_focus() -> None:
    evidence = _evidence()

    answer = EvidenceGrounder().ground(evidence)

    assert answer.claims[0].citation.row_ids == [7]
    assert answer.claims[0].citation.checksum == evidence.checksum


def test_grounder_rejects_direction_claims_from_union_tables() -> None:
    evidence = _evidence(artifact="union_significant_genes.csv")

    with pytest.raises(EvidenceGroundingError, match="union"):
        EvidenceGrounder().ground(evidence, _answer("GeneA is upregulated"))


def test_verifier_rejects_invalid_citation_numbers_and_causal_claims() -> None:
    verifier = AnswerVerifier()
    evidence = _evidence()

    numeric = verifier.verify(_answer("GeneA has PearsonR 0.99"), [evidence])
    missing_row = verifier.verify(_answer("GeneA has PearsonR 0.71", [99]), [evidence])
    causal = verifier.verify(_answer("GeneA causes M0123"), [evidence])

    assert numeric.verdict.value == "rejected"
    assert missing_row.verdict.value == "rejected"
    assert causal.verdict.value == "rejected"


def test_pipeline_repairs_once_then_falls_back_to_raw_evidence() -> None:
    evidence = _evidence()
    pipeline = GroundedAnswerPipeline()
    calls = []

    repaired = pipeline.answer(
        evidence,
        _answer("GeneA has PearsonR 0.99"),
        repair=lambda _draft, _verdict: calls.append("repair") or _answer("GeneA has PearsonR 0.71"),
    )
    assert calls == ["repair"]
    assert repaired.claims[0].text == "GeneA has PearsonR 0.71"

    fallback = pipeline.answer(
        evidence,
        _answer("GeneA causes M0123"),
        repair=lambda _draft, _verdict: _answer("GeneA causes M0123"),
    )
    assert fallback.claims[0].text.startswith("验证未通过")
    assert fallback.claims[1].citation.row_ids == [7]
