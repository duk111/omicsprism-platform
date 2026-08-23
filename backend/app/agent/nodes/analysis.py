from __future__ import annotations

import csv
import io
from collections.abc import Callable

from langgraph.graph import END
from langgraph.types import Command, interrupt

from ..fingerprint import compute_input_fingerprint
from ..graph import (
    AnalysisExecutionRequest,
    ClarificationItem,
    ClarificationPayload,
    ClarificationResume,
    ConfirmationPayload,
    ConfirmationResume,
    DatasetLoadRequest,
    DatasetLoader,
    GraphState,
    JobRef,
    JobSubmitter,
)
from ..param_resolver import AnalysisProposal, ResolvedRequest, resolve_analysis_request
from ..validation import DatasetRef, ValidationReport, validate_analysis_request


class DatasetLoadError(ValueError):
    """Loaded validation inputs do not match the ownership-bound state refs."""


class ExecutionRejected(ValueError):
    """Defensive execution checks rejected inputs changed after confirmation."""


def analysis_node(
    dataset_loader: DatasetLoader,
    job_submitter: JobSubmitter,
) -> Callable[[GraphState], Command]:
    def run(state: GraphState) -> Command:
        if isinstance(state.pending_interrupt, ConfirmationPayload):
            return _handle_confirmation(state, dataset_loader, job_submitter)

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
            if resolved.analysis_type is None or resolved.params is None:
                raise RuntimeError("successful validation must contain resolved parameters")
            payload = ConfirmationPayload(
                analysis_type=resolved.analysis_type,
                resolved_params=resolved.params,
                preview=report.preview,
                warnings=report.warnings,
                input_fingerprint=report.input_fingerprint,
            )
            return Command(
                update={
                    "resolved_request": resolved,
                    "validation_report": report,
                    "pending_interrupt": payload,
                    "response_text": None,
                    "step_budget": next_budget,
                },
                goto="analysis",
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


def _handle_confirmation(
    state: GraphState,
    dataset_loader: DatasetLoader,
    job_submitter: JobSubmitter,
) -> Command:
    payload = state.pending_interrupt
    if not isinstance(payload, ConfirmationPayload):
        raise RuntimeError("confirmation state is missing its typed payload")
    resumed = ConfirmationResume.model_validate(
        interrupt(payload.model_dump(mode="json"))
    )
    if resumed.action == "cancel":
        return Command(
            update={
                "pending_interrupt": None,
                "response_text": "Analysis cancelled.",
            },
            goto=END,
        )
    if resumed.action == "modify":
        return Command(
            update={
                "clarification_answer": resumed.modification,
                "pending_interrupt": None,
                "response_text": None,
            },
            goto="analysis",
        )

    if state.step_budget.used_steps >= state.step_budget.max_steps:
        return Command(
            update={
                "pending_interrupt": None,
                "response_text": "Analysis was not submitted because the step budget was exhausted.",
            },
            goto=END,
        )
    next_budget = state.step_budget.model_copy(
        update={"used_steps": state.step_budget.used_steps + 1}
    )
    try:
        job_ref = run_analysis(
            state, payload, resumed, dataset_loader, job_submitter
        )
    except (DatasetLoadError, ExecutionRejected) as exc:
        clarification = ClarificationPayload(
            missing=[ClarificationItem(
                field="input_fingerprint",
                reason=str(exc)[:500],
            )],
            question=(
                "The analysis inputs changed after validation. Review the current "
                "datasets and confirm the analysis request again."
            ),
        )
        answer = ClarificationResume.model_validate(
            interrupt(clarification.model_dump(mode="json"))
        )
        return Command(
            update={
                "clarification_answer": answer.answer,
                "pending_interrupt": clarification,
                "response_text": None,
                "step_budget": next_budget,
            },
            goto="analysis",
        )

    recent_jobs = [
        item for item in state.recent_jobs if item.job_id != job_ref.job_id
    ]
    recent_jobs.append(job_ref)
    return Command(
        update={
            "current_job": job_ref,
            "recent_jobs": recent_jobs[-20:],
            "pending_interrupt": None,
            "response_text": f"Analysis job {job_ref.job_id} was submitted.",
            "step_budget": next_budget,
        },
        goto=END,
    )


def run_analysis(
    state: GraphState,
    payload: ConfirmationPayload,
    resumed: ConfirmationResume,
    dataset_loader: DatasetLoader,
    job_submitter: JobSubmitter,
) -> JobRef:
    """Submit after low-cost ownership, fingerprint, and schema checks."""

    if resumed.idempotency_key is None:
        raise ExecutionRejected("run action is missing an idempotency key")
    dataset_refs = _load_owned_refs(state, dataset_loader)
    fingerprint = compute_input_fingerprint(
        owner_id=state.user_id,
        dataset_refs=dataset_refs,
        profiles=[item.profile for item in state.dataset_profiles],
    )
    if fingerprint.casefold() != payload.input_fingerprint.casefold():
        raise ExecutionRejected("input fingerprint no longer matches validation")
    _check_metadata_fields(payload, dataset_refs)
    request = AnalysisExecutionRequest(
        user_id=state.user_id,
        thread_id=state.thread_id,
        dataset_ids=[item.dataset_id for item in state.dataset_profiles],
        resolved_params=payload.resolved_params,
        input_fingerprint=payload.input_fingerprint,
        idempotency_key=resumed.idempotency_key,
    )
    job_ref = job_submitter(request)
    if job_ref.owner_id != state.user_id:
        raise ExecutionRejected("job submitter returned a cross-user job")
    return job_ref


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
    loaded = _load_owned_refs(state, dataset_loader)
    profiles = {item.dataset_id: item.profile for item in state.dataset_profiles}
    return [
        item.model_copy(update={"profile": profiles[item.dataset_id]})
        for item in loaded
    ]


def _load_owned_refs(
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
        normalized.append(item.model_copy(update={"profile": None}))
    return normalized


def _check_metadata_fields(
    payload: ConfirmationPayload,
    dataset_refs: list[DatasetRef],
) -> None:
    params = payload.resolved_params
    contrast = getattr(params, "contrast", None)
    if contrast is None:
        return
    metadata = next(
        (item for item in dataset_refs if item.role == "metadata"),
        None,
    )
    if metadata is None:
        raise ExecutionRejected("metadata dataset is no longer available")
    header = next(
        csv.reader(io.StringIO(metadata.content.decode("utf-8-sig", errors="replace"))),
        [],
    )
    available = {item.strip() for item in header}
    required = {contrast.compare_field, *contrast.same_fields}
    missing = sorted(required - available)
    if missing:
        raise ExecutionRejected(
            "metadata fields are no longer available: " + ", ".join(missing)
        )


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
