from __future__ import annotations

import csv
import io
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from langgraph.graph import END
from langgraph.types import Command, interrupt

from ..fingerprint import compute_input_fingerprint
from ..clarification_resolver import ClarificationOption, stable_option_id
from ..message_blocks import job_block, text_block
from ..graph import (
    AnalysisExecutionRequest,
    ConfirmationPayload,
    ConfirmationResume,
    DatasetLoadRequest,
    DatasetLoader,
    GraphState,
    JobRef,
    JobSubmitter,
    NodeCapabilityError,
    PendingPlan,
    PendingAnalysisClarification,
    PlanVersionConflict,
    StratumSummary,
)
from ..param_resolver import AnalysisProposal, ResolvedRequest, resolve_analysis_request
from ..validation import (
    DatasetRef,
    ValidationReport,
    derive_scoped_dataset_refs,
    validate_analysis_request,
)
from ...models import JobStatus


class DatasetLoadError(ValueError):
    """Loaded validation inputs do not match the ownership-bound state refs."""


class ExecutionRejected(ValueError):
    """Defensive execution checks rejected inputs changed after confirmation."""


def analysis_node(
    dataset_loader: DatasetLoader,
    job_submitter: JobSubmitter,
) -> Callable[[GraphState], Command]:
    def run(state: GraphState) -> Command:
        decision = state.decision
        if decision is None or decision.action not in {
            "inspect_dataset",
            "run_analysis",
            "propose_plan",
        }:
            action = decision.action if decision is not None else "missing"
            raise NodeCapabilityError(
                f"Analysis node does not allow action: {action}"
            )

        if isinstance(state.pending_interrupt, ConfirmationPayload):
            return _handle_confirmation(state, dataset_loader, job_submitter)

        if state.step_budget.used_model_steps >= state.step_budget.max_model_steps:
            return Command(
                update={
                    "response_text": "当前请求已达到执行步数上限，请重新发起分析请求。",
                },
                goto=END,
            )

        next_budget = state.step_budget.model_copy(
            update={"used_model_steps": state.step_budget.used_model_steps + 1}
        )
        dataset_refs = _load_validation_refs(state, dataset_loader)
        request_text = _analysis_request_text(state)
        resolved = resolve_analysis_request(
            request_text,
            [item.profile for item in state.dataset_profiles],
            llm_proposal=_analysis_proposal(state),
            prior_params=(
                state.pending_plan.params
                if state.pending_plan is not None
                else state.confirmed_params
            ),
        )
        report = validate_analysis_request(resolved, dataset_refs)
        if report.ok:
            if resolved.analysis_type is None or resolved.params is None:
                raise RuntimeError("successful validation must contain resolved parameters")
            pending_plan = _build_pending_plan(state, resolved, report)
            payload = ConfirmationPayload(
                analysis_type=resolved.analysis_type,
                resolved_params=resolved.params,
                preview=report.preview,
                warnings=report.warnings,
                input_fingerprint=report.input_fingerprint,
                plan_id=pending_plan.plan_id,
                plan_version=pending_plan.plan_version,
            )
            return Command(
                update={
                    "resolved_request": resolved,
                    "validation_report": report,
                    "pending_plan": pending_plan,
                    "pending_analysis": (
                        state.pending_analysis.model_copy(update={"status": "consumed"})
                        if state.pending_analysis is not None
                        else None
                    ),
                    "pending_interrupt": payload,
                    "response_text": None,
                    "step_budget": next_budget,
                },
                goto="analysis",
            )

        pending = PendingAnalysisClarification(
            analysis_type=resolved.analysis_type,
            question=_clarification_question(resolved, report),
            missing=[item.field for item in resolved.missing[:3]],
            options=[
                _clarification_option(item.field, option, state)
                for item in resolved.missing[:3]
                for option in item.options[:20]
            ][:20],
            source_message=state.user_message,
            input_bundle_id=state.active_input_bundle_id,
            source_action=state.decision.action,
            proposal=_analysis_proposal(state),
        )
        return Command(
            update={
                "response_text": pending.question,
                "resolved_request": resolved,
                "validation_report": report,
                "pending_analysis": pending,
                "step_budget": next_budget,
            },
            goto=END,
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
    _validate_plan_reference(state, payload, resumed)
    if resumed.approve is False:
        return Command(
            update={
                "pending_interrupt": None,
                "pending_plan": None,
                "response_text": "Analysis plan rejected.",
            },
            goto=END,
        )
    if resumed.approve is not True:
        if resumed.message is None:
            raise ExecutionRejected("confirmation message is required")
        return Command(
            update={
                "pending_interrupt": None,
                "decision": None,
                "user_message": resumed.message,
                "response_text": None,
            },
            goto="main",
        )

    if state.step_budget.used_model_steps >= state.step_budget.max_model_steps:
        return Command(
            update={
                "pending_interrupt": None,
                "response_text": "Analysis was not submitted because the step budget was exhausted.",
            },
            goto=END,
        )
    next_budget = state.step_budget.model_copy(
        update={"used_model_steps": state.step_budget.used_model_steps + 1}
    )
    try:
        job_ref = run_analysis(
            state, payload, resumed, dataset_loader, job_submitter
        )
    except (DatasetLoadError, ExecutionRejected) as exc:
        return Command(
            update={
                "pending_interrupt": None,
                "pending_plan": None,
                "response_text": (
                    "The analysis inputs changed before submission ("
                    f"{str(exc)[:400]}). Please review the current datasets and "
                    "start the analysis again."
                ),
                "step_budget": next_budget,
            },
            goto=END,
        )

    recent_jobs = [
        item for item in state.recent_jobs if item.job_id != job_ref.job_id
    ]
    recent_jobs.append(job_ref)
    return Command(
        update={
            "current_job": job_ref,
            "recent_jobs": recent_jobs[-20:],
            "confirmed_params": payload.resolved_params,
            "pending_interrupt": None,
            "pending_plan": None,
            "response_text": f"Analysis job {job_ref.job_id} was submitted.",
            "response_blocks": [
                text_block(f"Analysis job {job_ref.job_id} was submitted."),
                job_block(job_ref.job_id, JobStatus.QUEUED),
            ],
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
    dataset_refs = _load_validation_refs(state, dataset_loader)
    try:
        contrast = getattr(payload.resolved_params, "contrast", None)
        scope = contrast.scope if contrast is not None else None
        scoped_refs = (
            derive_scoped_dataset_refs(scope, dataset_refs)
            if scope is not None
            else [item.model_copy(deep=True) for item in dataset_refs]
        )
    except ValueError as exc:
        raise ExecutionRejected(str(exc)) from exc
    fingerprint = compute_input_fingerprint(
        owner_id=state.user_id,
        dataset_refs=scoped_refs,
        profiles=[item.profile for item in scoped_refs if item.profile is not None],
    )
    if fingerprint.casefold() != payload.input_fingerprint.casefold():
        raise ExecutionRejected("input fingerprint no longer matches validation")
    _check_metadata_fields(payload, dataset_refs)
    request = AnalysisExecutionRequest(
        user_id=state.user_id,
        thread_id=state.thread_id,
        trace_id=state.trace_id,
        turn_id=state.turn_id,
        run_id=state.run_id,
        dataset_ids=[item.dataset_id for item in state.dataset_profiles],
        resolved_params=payload.resolved_params,
        input_fingerprint=payload.input_fingerprint,
        idempotency_key=resumed.idempotency_key,
        scoped_inputs=scoped_refs if scope is not None and scope.mode == "fixed" else [],
    )
    job_ref = job_submitter(request)
    if job_ref.owner_id != state.user_id:
        raise ExecutionRejected("job submitter returned a cross-user job")
    return job_ref


def _validate_plan_reference(
    state: GraphState,
    payload: ConfirmationPayload,
    resumed: ConfirmationResume,
) -> None:
    if resumed.plan_id != payload.plan_id or resumed.plan_version != payload.plan_version:
        raise PlanVersionConflict("confirmation does not reference the current pending plan")
    if state.pending_plan is None or (
        state.pending_plan.plan_id != resumed.plan_id
        or state.pending_plan.plan_version != resumed.plan_version
    ):
        raise PlanVersionConflict("confirmation does not reference the current pending plan")


def _analysis_proposal(state: GraphState) -> AnalysisProposal:
    decision = state.decision
    proposal = decision.proposal if decision is not None else None
    analysis_type = decision.analysis_type if decision is not None else None
    if proposal is None:
        return AnalysisProposal(analysis_type=analysis_type)
    if proposal.analysis_type is None and analysis_type is not None:
        return proposal.model_copy(update={"analysis_type": analysis_type})
    return proposal


def _build_pending_plan(
    state: GraphState,
    resolved: ResolvedRequest,
    report: ValidationReport,
) -> PendingPlan:
    if resolved.analysis_type is None or resolved.params is None:
        raise ValueError("a pending plan requires resolved analysis parameters")
    contrast = getattr(resolved.params, "contrast", None)
    if contrast is None:
        raise ValueError("a pending plan requires a contrast")
    previous = state.pending_plan
    plan_id = previous.plan_id if previous is not None else f"plan-{uuid4().hex}"
    plan_version = previous.plan_version + 1 if previous is not None else 1
    sample_scope: list[StratumSummary] = []
    if report.preview is not None:
        sample_scope.append(StratumSummary(
            stratum=dict(report.preview.same_values),
            tested_count=report.preview.tested_count,
            reference_count=report.preview.reference_count,
        ))
    return PendingPlan(
        plan_id=plan_id,
        plan_version=plan_version,
        thread_id=state.thread_id,
        analysis_type=resolved.analysis_type,
        scope=contrast.scope,
        contrast=contrast,
        params=resolved.params,
        provenance=_plan_provenance(state, resolved.params),
        sample_scope=sample_scope,
        input_fingerprint=report.input_fingerprint,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=30),
    )


def _plan_provenance(state: GraphState, params: object) -> dict[str, str]:
    proposal = _analysis_proposal(state)
    result: dict[str, str] = {}
    if proposal.analysis_type is not None:
        result["analysis_type"] = "user_explicit"
    for name in ("compare_field", "tested_level", "reference_level"):
        if getattr(proposal, name) is not None:
            result[f"contrast.{name}"] = "user_explicit"
        else:
            result[f"contrast.{name}"] = "tool_derived"
    result["scope"] = (
        "user_explicit" if proposal.scope.mode != "unknown" else "tool_derived"
    )
    requested = set(proposal.requested_params)
    for name in getattr(type(params), "model_fields", {}):
        if name == "contrast":
            continue
        result[name] = "user_explicit" if name in requested else "system_default"
    return result


def _analysis_request_text(state: GraphState) -> str:
    return state.user_message


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
    required = {
        contrast.compare_field,
        *contrast.scope.fixed_filters,
        *contrast.scope.blocking_fields,
    }
    missing = sorted(required - available)
    if missing:
        raise ExecutionRejected(
            "metadata fields are no longer available: " + ", ".join(missing)
        )


def _clarification_question(
    resolved: ResolvedRequest,
    report: ValidationReport,
) -> str:
    if resolved.missing:
        question = resolved.clarification or "请补充缺失的分析参数。"
        options = [
            f"{item.field}: {', '.join(item.options[:20])}"
            for item in resolved.missing[:3]
            if item.options
        ]
        if options:
            question = f"{question} 可选项：" + "；".join(options)
    else:
        details = "；".join(item.message[:500] for item in report.blocking[:3])
        question = f"分析请求未通过校验，请处理后继续：{details}"
    return question[:1200]


def _clarification_option(
    field: str,
    label: str,
    state: GraphState,
) -> ClarificationOption:
    """Attach a stable, deterministic proposal patch to a visible option."""

    patch: dict[str, object] = {}
    text = str(label).strip()
    if field == "contrast":
        # ``_ambiguous_request`` emits ``field: tested vs reference``.  Keep
        # this parser deliberately strict; an unparsed label remains a visible
        # option but cannot be executed without another deterministic check.
        if ":" in text and " vs " in text:
            compare_field, pair = text.split(":", 1)
            tested, reference = pair.split(" vs ", 1)
            reference = reference.split("，", 1)[0].split(",", 1)[0].strip()
            patch.update({
                "compare_field": compare_field.strip(),
                "tested_level": tested.strip(),
                "reference_level": reference.strip(),
            })
    elif field in {"compare_field", "tested_level", "reference_level"}:
        patch[field] = text
    elif field == "scope" and text in {"all", "stratified", "fixed"}:
        patch["scope_mode"] = text
    option_id = stable_option_id(text, patch)
    return ClarificationOption(option_id=option_id, label=text, proposal_patch=patch)
