"""Deterministic role routing for the multi-agent protocol boundary."""

from __future__ import annotations

from .graph import AgentRole, GraphState


def route(state: GraphState) -> AgentRole:
    """Select an agent role using bounded, deterministic intent rules."""

    message = state.user_message
    has_jobs = bool(state.current_job or state.recent_jobs)
    pending = state.pending_analysis

    if pending is not None and pending.status == "active":
        if _is_explicit_knowledge_question(message):
            return AgentRole.QA
        if has_jobs and _is_explicit_result_request(message):
            return AgentRole.RESULT_QA
        if _is_explicit_analysis_request(message) or _is_analysis_followup(message):
            return AgentRole.ANALYSIS
        return AgentRole.ANALYSIS

    if has_jobs and _is_explicit_result_request(message):
        return AgentRole.RESULT_QA

    if _is_explicit_analysis_request(message) or _is_analysis_followup(message):
        return AgentRole.ANALYSIS

    if _is_new_dataset_message(message):
        return AgentRole.ANALYSIS

    return AgentRole.QA


def _is_explicit_result_request(message: str) -> bool:
    text = message.casefold()
    return (
        _is_explicit_jobs_listing(message)
        or _is_explicit_job_status_request(message)
        or any(
            marker in text
            for marker in (
                "result", "artifact", "evidence", "fold change", "log2fc", "padj",
                "\u7ed3\u679c", "\u4ea7\u7269", "\u8bc1\u636e",
            )
        )
    )


def _is_explicit_knowledge_question(message: str) -> bool:
    text = message.casefold().strip()
    markers = (
        "what is ", "what are ", "what does ", "define ", "explain ", "why ",
        "how does ", "how do ", "\u4ec0\u4e48\u662f", "\u4ec0\u4e48\u53eb", "\u5982\u4f55",
        "\u4e3a\u4ec0\u4e48", "\u539f\u7406", "\u6982\u5ff5", "\u89e3\u91ca",
    )
    return any(marker in text for marker in markers)


def _is_analysis_followup(message: str) -> bool:
    text = message.casefold().strip()
    return any(
        marker in text
        for marker in (
            "continue the analysis", "continue analysis", "resume the analysis", "resume analysis",
            "\u7ee7\u7eed\u521a\u624d\u5206\u6790", "\u7ee7\u7eed\u5206\u6790", "\u6062\u590d\u5206\u6790", "\u63a5\u7740\u5206\u6790",
        )
    )


def _is_new_dataset_message(message: str) -> bool:
    text = message.casefold().strip()
    return any(
        marker in text
        for marker in (
            "upload new", "uploaded new", "re-upload", "reupload", "new dataset", "new data",
            "\u91cd\u65b0\u4e0a\u4f20", "\u91cd\u65b0\u4e0a\u4f20\u6570\u636e", "\u4e0a\u4f20\u65b0\u6570\u636e", "\u6362\u4e00\u6279\u6570\u636e",
        )
    )


def _is_explicit_analysis_request(message: str) -> bool:
    return any(
        marker in message.casefold()
        for marker in (
            "run ", "analyze", "compare ", "plan ", "execute", "perform ", "start ",
            "\u5206\u6790", "\u8fd0\u884c", "\u6bd4\u8f83", "\u8ba1\u5212",
        )
    )


def _is_explicit_job_status_request(message: str) -> bool:
    text = message.casefold()
    if any(marker in text for marker in ("unavailable", "internal", "attempt")):
        return False
    return any(marker in text for marker in ("status", "progress", "running", "queued", "job"))


def _is_explicit_jobs_listing(message: str) -> bool:
    text = message.casefold().strip()
    markers = (
        "list available jobs", "list jobs", "show available jobs", "show jobs", "available jobs",
        "\u5217\u51fa\u4efb\u52a1", "\u53ef\u7528\u4efb\u52a1", "\u6709\u54ea\u4e9b\u4efb\u52a1",
    )
    return any(marker in text for marker in markers)


__all__ = [
    "route",
    "_is_explicit_analysis_request",
    "_is_explicit_job_status_request",
    "_is_explicit_jobs_listing",
]
