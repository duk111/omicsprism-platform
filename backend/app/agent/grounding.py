from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .schemas import Citation, GroundedAnswer, GroundedClaim, RunState, ToolName, ToolResult
from .verifier import AnswerVerifier


NO_EVIDENCE_TEXT = "没有满足阈值的证据"
FALLBACK_TEXT = "验证未通过，以下为原始证据行，不作解释。"


class EvidenceGroundingError(ValueError):
    """草稿未绑定到本次工具返回的证据。"""


class EvidenceGrounder:
    """将回答草稿限制为当前只读工具调用返回的行。"""

    def ground(self, evidence: ToolResult, draft: GroundedAnswer | None = None) -> GroundedAnswer:
        self._validate_evidence(evidence)
        indexed_rows = _indexed_rows(evidence)
        if not indexed_rows:
            return GroundedAnswer(claims=[GroundedClaim(
                text=NO_EVIDENCE_TEXT,
                citation=Citation(
                    artifact=evidence.artifact or "query_result_evidence",
                    checksum=evidence.checksum or "sha256:empty",
                    row_ids=[],
                ),
            )])

        candidate = draft or self._raw_evidence_answer(evidence, indexed_rows)
        self._reject_union_direction_claims(evidence, candidate)
        self._validate_citations(evidence, candidate, indexed_rows)
        return candidate

    def fallback(self, evidence: ToolResult) -> GroundedAnswer:
        """第二次验证失败时，仅展示原始行并标注不作解释。"""
        raw = self.ground(evidence)
        if not raw.claims or raw.claims[0].text == NO_EVIDENCE_TEXT:
            return raw
        return GroundedAnswer(claims=[
            GroundedClaim(text=FALLBACK_TEXT, citation=raw.claims[0].citation),
            *raw.claims,
        ])

    def update_focus(self, state: RunState, answer: GroundedAnswer) -> None:
        if answer.claims:
            state.focus.last_citation = answer.claims[-1].citation

    def _validate_evidence(self, evidence: ToolResult) -> None:
        if evidence.tool is not ToolName.QUERY_RESULT_EVIDENCE or not evidence.ok:
            raise EvidenceGroundingError("grounding requires successful query_result_evidence")
        if not evidence.artifact or not evidence.checksum:
            raise EvidenceGroundingError("evidence requires artifact and checksum")

    def _raw_evidence_answer(
        self,
        evidence: ToolResult,
        indexed_rows: list[tuple[int, dict[str, Any]]],
    ) -> GroundedAnswer:
        claims = []
        for row_id, row in indexed_rows:
            fields = "; ".join(
                f"{key}={value}" for key, value in row.items() if key != "_row_id"
            )
            claims.append(GroundedClaim(
                text=fields,
                citation=Citation(
                    artifact=evidence.artifact or "",
                    checksum=evidence.checksum or "",
                    row_ids=[row_id],
                ),
            ))
        return GroundedAnswer(claims=claims)

    def _validate_citations(
        self,
        evidence: ToolResult,
        answer: GroundedAnswer,
        indexed_rows: list[tuple[int, dict[str, Any]]],
    ) -> None:
        valid_ids = {row_id for row_id, _ in indexed_rows}
        for claim in answer.claims:
            citation = claim.citation
            if citation.artifact != evidence.artifact or citation.checksum != evidence.checksum:
                raise EvidenceGroundingError("claim citation does not match the current evidence")
            if not citation.row_ids or not set(citation.row_ids).issubset(valid_ids):
                raise EvidenceGroundingError("claim citation has no valid evidence row")

    def _reject_union_direction_claims(self, evidence: ToolResult, answer: GroundedAnswer) -> None:
        if not evidence.artifact or not evidence.artifact.endswith("union_significant_genes.csv"):
            return
        direction_terms = ("upregulated", "downregulated", "上调", "下调", "up-regulated", "down-regulated")
        if any(term in claim.text.lower() for claim in answer.claims for term in direction_terms):
            raise EvidenceGroundingError("union evidence cannot support direction claims")


class GroundedAnswerPipeline:
    """验证失败只修复一次，仍失败则降级为原始证据展示。"""

    def __init__(self, *, grounder: EvidenceGrounder | None = None, verifier: AnswerVerifier | None = None) -> None:
        self.grounder = grounder or EvidenceGrounder()
        self.verifier = verifier or AnswerVerifier()

    def answer(
        self,
        evidence: ToolResult,
        draft: GroundedAnswer | None = None,
        repair: Callable[[GroundedAnswer, Any], GroundedAnswer] | None = None,
    ) -> GroundedAnswer:
        # 模型草稿引用非法（错 artifact/checksum、行号不在本轮证据里）是常见的
        # 语义错误，不是系统故障：降级到原始证据展示，不要把整个 turn 打掉。
        try:
            candidate = self.grounder.ground(evidence, draft)
        except EvidenceGroundingError:
            return self.grounder.fallback(evidence)
        verdict = self.verifier.verify(candidate, [evidence])
        if verdict.verdict.value == "approved":
            return candidate
        if repair is not None:
            try:
                repaired = self.grounder.ground(evidence, repair(candidate, verdict))
            except EvidenceGroundingError:
                repaired = None
            if repaired is not None and self.verifier.verify(repaired, [evidence]).verdict.value == "approved":
                return repaired
        return self.grounder.fallback(evidence)


def _indexed_rows(evidence: ToolResult) -> list[tuple[int, dict[str, Any]]]:
    indexed: list[tuple[int, dict[str, Any]]] = []
    seen: set[int] = set()
    for position, row in enumerate(evidence.rows, start=1):
        raw_id = row.get("_row_id", position)
        try:
            row_id = int(raw_id)
        except (TypeError, ValueError) as exc:
            raise EvidenceGroundingError("evidence row id must be an integer") from exc
        if row_id <= 0 or row_id in seen:
            raise EvidenceGroundingError("evidence row ids must be unique positive integers")
        seen.add(row_id)
        indexed.append((row_id, row))
    return indexed
