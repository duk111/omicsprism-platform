from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Sequence

from .schemas import GroundedAnswer, ToolResult, VerifierCheck, VerifierDecision, VerifierVerdict


_NUMBER = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?(?![\w.])")
_CAUSAL_TERMS = ("导致", "因果", "证明", "causes", "cause of", "proves")


class AnswerVerifier:
    """无工具、无句柄的确定性证据核查器。"""

    def verify(self, answer: GroundedAnswer, evidence: Sequence[ToolResult]) -> VerifierVerdict:
        checks = []
        for index, claim in enumerate(answer.claims):
            cited_rows = _cited_rows(claim.citation.artifact, claim.citation.checksum, claim.citation.row_ids, evidence)
            citation_valid = cited_rows is not None
            if cited_rows is None:
                cited_rows = []
            number_matches = citation_valid and _numbers_match(claim.text, cited_rows)
            beyond_evidence = _contains_causal_claim(claim.text)
            issues = []
            if not citation_valid:
                issues.append("citation does not identify returned evidence rows")
            if not number_matches:
                issues.append("claim number is not present in cited evidence")
            if beyond_evidence:
                issues.append("claim makes a causal assertion beyond evidence")
            checks.append(VerifierCheck(
                claim_index=index,
                number_matches_evidence=number_matches,
                citation_valid=citation_valid,
                beyond_evidence=beyond_evidence,
                issues=issues,
            ))
        approved = bool(checks) and all(
            check.number_matches_evidence and check.citation_valid and not check.beyond_evidence
            for check in checks
        )
        return VerifierVerdict(
            verdict=VerifierDecision.APPROVED if approved else VerifierDecision.REJECTED,
            checks=checks,
        )


def _cited_rows(artifact: str, checksum: str, row_ids: list[int], evidence: Sequence[ToolResult]) -> list[dict[str, Any]] | None:
    matching = [item for item in evidence if item.ok and item.artifact == artifact and item.checksum == checksum]
    if len(matching) != 1:
        return None
    selected = matching[0]
    if not row_ids:
        return [] if not selected.rows else None
    rows_by_id: dict[int, dict[str, Any]] = {}
    for position, row in enumerate(selected.rows, start=1):
        try:
            row_id = int(row.get("_row_id", position))
        except (TypeError, ValueError):
            return None
        rows_by_id[row_id] = row
    if not set(row_ids).issubset(rows_by_id):
        return None
    return [rows_by_id[row_id] for row_id in row_ids]


def _numbers_match(text: str, rows: Sequence[dict[str, Any]]) -> bool:
    requested = [_decimal(value) for value in _NUMBER.findall(text)]
    requested = [value for value in requested if value is not None]
    available = {
        value
        for row in rows
        for cell in row.values()
        for value in [_decimal(str(cell))]
        if value is not None
    }
    return all(value in available for value in requested)


def _decimal(value: str) -> Decimal | None:
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def _contains_causal_claim(text: str) -> bool:
    normalized = text.lower()
    return any(term in normalized for term in _CAUSAL_TERMS)
