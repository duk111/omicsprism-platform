from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from .schemas import Citation, GroundedAnswer, GroundedClaim, RunState, ToolName, ToolResult
from .verifier import AnswerVerifier, FALLBACK_TEXT, NO_EVIDENCE_TEXT, raw_evidence_text


class EvidenceGroundingError(ValueError):
    """草稿未绑定到本次工具返回的证据。"""


def fallback_query_context(answer: GroundedAnswer, evidence: ToolResult | None) -> str:
    """Render executed query conditions as data, never as model assertions."""
    if evidence is None or not answer.claims or answer.claims[0].text != FALLBACK_TEXT:
        return ""
    conditions = json.dumps({"filters": evidence.filters, "sort": evidence.sort,
                             "row_count": evidence.row_count, "truncated": evidence.truncated}, ensure_ascii=False)
    return "查询条件：" + conditions[:400] + "\n"


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
            *raw.claims[:49],
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
            fields = raw_evidence_text(row)
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
        user_message: str = "",
    ) -> GroundedAnswer:
        # 模型草稿引用非法（错 artifact/checksum、行号不在本轮证据里）是常见的
        # 语义错误，不是系统故障：降级到原始证据展示，不要把整个 turn 打掉。
        try:
            candidate = self.grounder.ground(evidence, draft or _concise_draft(evidence, user_message))
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


def _concise_draft(evidence: ToolResult, user_message: str) -> GroundedAnswer | None:
    """Project explicitly requested numeric fields; the original verifier still decides."""
    if not user_message or not evidence.rows:
        return None
    from .verifier import _decimal

    chinese = any("\u4e00" <= char <= "\u9fff" for char in user_message)
    claims = []
    indexed = _indexed_rows(evidence)
    # Keep multi-row result sets as canonical raw evidence. A compact claim is
    # only safe when the user asked for a single entity/row's fields.
    if len(indexed) != 1:
        return None
    for row_id, row in indexed:
        # Preserve both endpoints and string annotations of network rows.
        entities = [key for key in ("gene_id", "metabolite_id", "Gene", "Metabolite") if key in row]
        if len(entities) != 1:
            return None
        fields = [key for key in row if key != "_row_id" and key not in entities
                  and re.search(rf"(?<![A-Za-z0-9_]){re.escape(key)}(?![A-Za-z0-9_])", user_message, re.I)
                  and _decimal(str(row[key])) is not None]
        if not fields:
            return None
        entity = str(row[entities[0]])
        text = (entity + " 的 " + "，".join(f"{field} 为 {row[field]}" for field in fields) + "。"
                if chinese else entity + " has " + " and ".join(f"{field} {row[field]}" for field in fields) + ".")
        claims.append(GroundedClaim(text=text, citation=Citation(
            artifact=evidence.artifact or "", checksum=evidence.checksum or "", row_ids=[row_id])))
    return GroundedAnswer(claims=claims)


def metric_explanation(user_message: str, evidence: ToolResult | None) -> str:
    """General definitions only, never an interpretation of an individual row."""
    if evidence is None or not evidence.ok or not evidence.rows:
        return ""
    wants_explanation = re.search(r"解释|代表|含义|意味|因果|证明|explain|interpret|mean|caus|prov", user_message, re.I)
    if not wants_explanation:
        return ""
    chinese = any("\u4e00" <= char <= "\u9fff" for char in user_message)
    definitions = {
        "log2FoldChange": ("log2FoldChange 表示相对参考组、以 2 为底的对数倍数变化；正负方向取决于 tested/reference 的定义。",
                           "log2FoldChange is the base-two log fold change relative to the reference; its sign depends on the tested/reference definition."),
        "padj": ("padj 是多重检验校正后的 p 值，不表示变化幅度。",
                 "padj is a p-value adjusted for multiple testing, not a measure of effect size."),
        "padj_bh": ("padj_bh 是经 Benjamini–Hochberg 方法校正的 p 值，不表示变化幅度。",
                    "padj_bh is a Benjamini–Hochberg adjusted p-value, not a measure of effect size."),
        "PearsonR": ("PearsonR 衡量线性相关性；相关性本身不能证明因果关系或调控机制。",
                     "PearsonR measures linear correlation; correlation alone does not establish causation or a regulatory mechanism."),
    }
    fields = {key for row in evidence.rows for key in row}
    notes = [pair[0 if chinese else 1] for field, pair in definitions.items()
             if field in fields and re.search(rf"(?<![A-Za-z0-9_]){re.escape(field)}(?![A-Za-z0-9_])", user_message, re.I)]
    return " ".join(notes)


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
