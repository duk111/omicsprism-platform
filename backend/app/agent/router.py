"""Deterministic role routing for the multi-agent protocol boundary."""

from __future__ import annotations

from .graph import AgentRole, GraphState, RouteTarget


def route(state: GraphState) -> AgentRole | RouteTarget | None:
    """Return a high-confidence deterministic role, or defer to the classifier."""

    message = state.user_message
    has_jobs = bool(state.current_job or state.recent_jobs)
    pending = state.pending_analysis

    if state.decision is not None and state.decision.action == "reroute":
        target = state.decision.reroute_to
        if target is not None:
            return AgentRole(target)

    if state.turn_origin == "job_continuation":
        return AgentRole.RESULT_QA

    if pending is not None and pending.status == "active":
        if _is_explicit_knowledge_question(message):
            return AgentRole.QA
        if has_jobs and _is_explicit_result_request(message):
            return AgentRole.RESULT_QA
        if _is_explicit_analysis_request(message) or _is_analysis_followup(message):
            return AgentRole.ANALYSIS
        return AgentRole.ANALYSIS

    if _is_explicit_jobs_listing(message):
        return AgentRole.RESULT_QA

    if has_jobs and _is_explicit_result_request(message):
        return AgentRole.RESULT_QA

    if _is_capability_request(message):
        return AgentRole.ANALYSIS

    if _is_explicitly_unsupported(message):
        return RouteTarget.UNSUPPORTED

    if _is_explicit_analysis_request(message) or _is_analysis_followup(message):
        return AgentRole.ANALYSIS

    if _is_new_dataset_message(message):
        return AgentRole.ANALYSIS

    if _needs_semantic_classification(message, state):
        return None

    return AgentRole.QA


def _is_explicitly_unsupported(message: str) -> bool:
    text = message.casefold()
    return any(marker in text for marker in (
        "single-cell", "single cell", "空间转录组", "spatial transcriptomics",
        "蛋白质结构", "protein structure", "image segmentation", "图像分割",
    ))


def _needs_semantic_classification(message: str, state: GraphState) -> bool:
    text = message.casefold().strip()
    vague_data_requests = (
        "帮我看看", "帮我看一下", "看看这些数据", "看下这些数据",
        "这两个数据", "这些数据能", "这些数据可以", "这组数据",
        "look at these data", "take a look at this dataset", "what can these data",
    )
    return bool(state.dataset_profiles) and any(marker in text for marker in vague_data_requests)


def _is_explicit_result_request(message: str) -> bool:
    text = message.casefold()
    return (
        _is_explicit_jobs_listing(message)
        or _is_explicit_job_status_request(message)
        or any(
            marker in text
            for marker in (
                "result", "artifact", "evidence", "fold change", "log2fc", "padj",
                "what happened to", "show ", "query ",
                "\u67e5\u8be2\u7ed3\u679c", "\u67e5\u770b\u7ed3\u679c", "\u7ed3\u679c\u662f",
                "\u7ed3\u679c\u4e2d", "\u4ea7\u7269", "\u8bc1\u636e",
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


def _is_capability_request(message: str) -> bool:
    text = message.casefold().strip()
    return any(marker in text for marker in (
        "能做什么", "可以做什么", "能分析什么", "可以分析什么",
        "能做差异分析", "可以做差异分析", "能做 deg", "可以做 deg",
        "what can", "what analyses", "supported analysis", "can i do",
    ))


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
            "run ", "analyze", "compare ", "inspect ", "metadata", "contrast", "plan ", "execute", "perform ", "start ",
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
    "_is_capability_request",
]
