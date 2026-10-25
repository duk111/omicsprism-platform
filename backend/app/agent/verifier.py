from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Sequence

from .schemas import GroundedAnswer, ToolName, ToolResult, VerifierCheck, VerifierDecision, VerifierVerdict


_NUMBER = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?(?![\w.])")
_CAUSAL_TERMS = ("导致", "因果", "证明", "causes", "cause of", "proves")
NO_EVIDENCE_TEXT = "没有满足阈值的证据"
FALLBACK_TEXT = "验证未通过，以下为原始证据行，不作解释。"
_ENTITY_FIELDS = {"gene_id", "metabolite_id", "Gene", "Metabolite", "Source", "Target"}
_SCALAR_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"


class AnswerVerifier:
    """无工具、无句柄的确定性证据核查器。"""

    def verify(self, answer: GroundedAnswer, evidence: Sequence[ToolResult]) -> VerifierVerdict:
        checks = []
        for index, claim in enumerate(answer.claims):
            cited_rows = _cited_rows(claim.citation.artifact, claim.citation.checksum, claim.citation.row_ids, evidence)
            citation_valid = cited_rows is not None
            if cited_rows is None:
                cited_rows = []
            raw_row = len(cited_rows) == 1 and claim.text == raw_evidence_text(cited_rows[0])
            number_matches = citation_valid and (raw_row or _numbers_match(claim.text, cited_rows))
            binding_valid = citation_valid and _claim_matches_row(claim.text, cited_rows)
            beyond_evidence = not binding_valid or (not raw_row and _contains_causal_claim(claim.text))
            issues = []
            if not citation_valid:
                issues.append("citation does not identify returned evidence rows")
            if not number_matches:
                issues.append("claim number is not present in cited evidence")
            if beyond_evidence:
                issues.append("claim entity/field/value is not bound to one cited row, or asserts beyond evidence")
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
    matching = [item for item in evidence if item.ok and item.artifact and item.checksum
                and item.tool in {ToolName.QUERY_RESULT_EVIDENCE, ToolName.QUERY_ARTIFACT}
                and item.artifact == artifact and item.checksum == checksum]
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
        if row_id <= 0 or row_id in rows_by_id or isinstance(row.get("_row_id"), bool):
            return None
        rows_by_id[row_id] = row
    if not set(row_ids).issubset(rows_by_id):
        return None
    return [rows_by_id[row_id] for row_id in row_ids]


def raw_evidence_text(row: dict[str, Any]) -> str:
    """Shared canonical rendering; IDs and string cells are preserved verbatim."""
    return "; ".join(f"{key}={value}" for key, value in row.items() if key != "_row_id")


def _claim_matches_row(text: str, rows: Sequence[dict[str, Any]]) -> bool:
    # No natural-language inference: unknown grammar, entities, fields and
    # multi-row attribution fail closed. The pipeline can still display raw rows.
    if not rows:
        return text == NO_EVIDENCE_TEXT
    if text == FALLBACK_TEXT:
        return True
    if len(rows) != 1:
        return False
    row = rows[0]
    if text == raw_evidence_text(row):
        return True
    for entity_field in _ENTITY_FIELDS & row.keys():
        entity = str(row[entity_field])
        if not entity:
            continue
        prefix = re.match(rf"{re.escape(entity)}(?:\s+has\s+|\s*的\s*)", text)
        if prefix is None:
            continue
        remainder = text[prefix.end():].rstrip("。.")
        parts = re.split(r"\s+and\s+|\s*[;,，；]\s*", remainder)
        if not parts or any(not part for part in parts):
            return False
        for part in parts:
            match = re.fullmatch(rf"([A-Za-z_][A-Za-z0-9_]*)\s*(?:=|:|为|是|is\s+)?\s*({_SCALAR_NUMBER})", part)
            if match is None:
                break
            field, value = match.groups()
            actual = _decimal(str(row.get(field, "")))
            if field == "_row_id" or field in _ENTITY_FIELDS or actual is None or actual != _decimal(value):
                break
        else:
            return True
    return False


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
    if not all(value in available for value in requested):
        return False
    normalized_text = text.lower()
    for row in rows:
        for field, cell in row.items():
            if field == "_row_id" or str(field).lower() not in normalized_text:
                continue
            field_values = {
                value for value in [_decimal(str(cell))] if value is not None
            }
            pattern = re.compile(
                rf"{re.escape(str(field))}\s*(?:为|是|=|:|is)?\s*({_NUMBER.pattern})",
                re.IGNORECASE,
            )
            mentioned = [value for value in (_decimal(match) for match in pattern.findall(text)) if value is not None]
            if mentioned and not all(value in field_values for value in mentioned):
                return False
    return True


def _decimal(value: str) -> Decimal | None:
    try:
        number = Decimal(value)
        return number if number.is_finite() else None
    except InvalidOperation:
        return None


def _contains_causal_claim(text: str) -> bool:
    normalized = text.lower()
    return any(term in normalized for term in _CAUSAL_TERMS)
