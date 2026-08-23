from __future__ import annotations

from collections.abc import Callable

from langgraph.graph import END
from langgraph.types import Command, interrupt

from ..graph import (
    ClarificationItem,
    ClarificationPayload,
    ClarificationResume,
    DatasetLoadRequest,
    DatasetLoader,
    GraphState,
)
from ..param_resolver import AnalysisProposal, ResolvedRequest, resolve_analysis_request
from ..validation import DatasetRef, ValidationReport, validate_analysis_request


class DatasetLoadError(ValueError):
    """Loaded validation inputs do not match the ownership-bound state refs."""


def analysis_node(dataset_loader: DatasetLoader) -> Callable[[GraphState], Command]:
    def run(state: GraphState) -> Command:
        if state.step_budget.used_steps >= state.step_budget.max_steps:
            return Command(
                update={
                    "response_text": "当前请求已达到执行步数上限，请重新发起分析请求。",
                },
                goto=END,
            )

        next_budget = state.step_budget.model_copy(
            update={"used_steps": state.step_budget.used_steps + 1}
        )
        dataset_refs = _load_validation_refs(state, dataset_loader)
        request_text = _analysis_request_text(state)
        resolved = resolve_analysis_request(
            request_text,
            [item.profile for item in state.dataset_profiles],
            llm_proposal=_analysis_proposal(state),
        )
        report = validate_analysis_request(resolved, dataset_refs)
        if report.ok:
            return Command(
                update={
                    "resolved_request": resolved,
                    "validation_report": report,
                    "pending_interrupt": None,
                    "response_text": None,
                    "step_budget": next_budget,
                },
                goto=END,
            )

        payload = _clarification_payload(resolved, report)
        resumed = ClarificationResume.model_validate(
            interrupt(payload.model_dump(mode="json"))
        )
        return Command(
            update={
                "clarification_answer": resumed.answer,
                "resolved_request": resolved,
                "validation_report": report,
                "pending_interrupt": payload,
                "step_budget": next_budget,
            },
            goto="analysis",
        )

    return run


def _analysis_proposal(state: GraphState) -> AnalysisProposal:
    decision = state.decision
    proposal = decision.proposal if decision is not None else None
    analysis_type = decision.analysis_type if decision is not None else None
    if proposal is None:
        return AnalysisProposal(analysis_type=analysis_type)
    if proposal.analysis_type is None and analysis_type is not None:
        return proposal.model_copy(update={"analysis_type": analysis_type})
    return proposal


def _analysis_request_text(state: GraphState) -> str:
    if state.clarification_answer is None:
        return state.user_message
    return f"{state.user_message}\n用户补充：{state.clarification_answer}"


def _load_validation_refs(
    state: GraphState,
    dataset_loader: DatasetLoader,
) -> list[DatasetRef]:
    expected = {item.dataset_id: item for item in state.dataset_profiles}
    request = DatasetLoadRequest(
        user_id=state.user_id,
        dataset_ids=list(expected),
    )
    loaded = dataset_loader(request)
    if len({item.dataset_id for item in loaded}) != len(loaded):
        raise DatasetLoadError("dataset loader returned duplicate ids")
    loaded_by_id = {item.dataset_id: item for item in loaded}
    if set(loaded_by_id) != set(expected):
        raise DatasetLoadError("dataset loader returned a different dataset set")

    normalized: list[DatasetRef] = []
    for dataset_id, profile_ref in expected.items():
        item = loaded_by_id[dataset_id]
        if item.owner_id != state.user_id:
            raise DatasetLoadError("dataset loader returned a cross-user dataset")
        if item.role != profile_ref.profile.role:
            raise DatasetLoadError("dataset loader returned a different dataset role")
        if item.checksum.casefold() != profile_ref.checksum.casefold():
            raise DatasetLoadError("dataset loader returned a different dataset checksum")
        normalized.append(item.model_copy(update={"profile": profile_ref.profile}))
    return normalized


def _clarification_payload(
    resolved: ResolvedRequest,
    report: ValidationReport,
) -> ClarificationPayload:
    if resolved.missing:
        items = [
            ClarificationItem(
                field=item.field[:200],
                options=item.options[:20],
                reason=item.reason[:500],
            )
            for item in resolved.missing[:3]
        ]
        question = resolved.clarification or "请补充缺失的分析参数。"
    else:
        items = [
            ClarificationItem(
                field=(item.field or item.code)[:200],
                reason=item.message[:500],
            )
            for item in report.blocking[:3]
        ]
        details = "；".join(item.reason for item in items)
        question = f"分析请求未通过校验，请处理后继续：{details}"
    return ClarificationPayload(
        missing=items,
        question=question[:1000],
    )
