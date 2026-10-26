"""Bounded result locators shared by the existing router and result nodes."""
from __future__ import annotations

import re

from .context import ResultSelection
from .readonly_tools import QueryArtifactRequest
from .schemas import ToolResult


def job_scope(state: object) -> list[str]:
    focus = getattr(state, "focus", None)
    ids = getattr(focus, "in_scope_job_ids", []) or []
    if ids:
        return list(dict.fromkeys(ids))
    current = getattr(state, "current_job", None)
    if current is not None:
        return [current.job_id]
    return list(dict.fromkeys(job.job_id for job in getattr(state, "recent_jobs", [])))


def is_result_followup(message: str) -> bool:
    """Only deictic expressions reuse a selection, never a newly named entity."""
    if re.search(r"\b(?:Gene|Met)[A-Za-z0-9_-]+\b", message, re.IGNORECASE):
        return False
    return bool(re.search(
        r"其中|它(?:们)?|这个|刚才|上述|这(?:些|五个)|"
        r"\b(?:its|their|among them|of (?:these|those|them)|that result|these results)\b",
        message, re.IGNORECASE,
    ))


def selection_from_evidence(user_id: str, arguments: dict, evidence: ToolResult) -> ResultSelection:
    """Accept a typed query locator only after the ownership-bound tool succeeds."""
    request = QueryArtifactRequest.model_validate(arguments)
    entities = {}
    for field in ("gene_id", "Gene", "metabolite_id", "Metabolite"):
        values = list(dict.fromkeys(str(row[field]) for row in evidence.rows if row.get(field) not in {None, ""}))
        if values:
            entities[field] = values[:50]
    ids = [int(row.get("_row_id", index)) for index, row in enumerate(evidence.rows, 1)]
    if any(row_id <= 0 for row_id in ids) or len(ids) != len(set(ids)):
        raise ValueError("result locator requires unique positive evidence row ids")
    return ResultSelection(
        job_id=request.job_id, owner_id=user_id, artifact=evidence.artifact or request.artifact,
        checksum=evidence.checksum or "", arguments=request.model_dump(mode="json", exclude_none=True),
        row_ids=ids, selected_row_ids=ids, entities=entities,
    )


def selection_matches_scope(state: object) -> bool:
    selection = getattr(state, "result_selection", None)
    return (selection is not None and selection.owner_id == getattr(state, "user_id", None)
            and job_scope(state) == [selection.job_id]
            and selection.arguments.get("job_id") == selection.job_id
            and selection.arguments.get("artifact") in {selection.artifact, selection.artifact.rsplit("/", 1)[-1]})
