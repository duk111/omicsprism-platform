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
    AgentRole,
    AnalysisModelOutput,
    AnalysisExecutionRequest,
    ConfirmationPayload,
    ConfirmationResume,
    DatasetLoadRequest,
    DatasetLoader,
    GraphState,
    JobRef,
    JobSubmitter,
    MainDecisionModel,
    NodeCapabilityError,
    PendingPlan,
    PendingAnalysisClarification,
    PlanVersionConflict,
    StratumSummary,
)
from ..param_resolver import (
    AnalysisProposal,
    ResolvedRequest,
    ScopeSpec,
    infer_analysis_type,
    resolve_analysis_request,
)
from ..validation import (
    DatasetRef,
    ValidationReport,
    derive_scoped_dataset_refs,
    validate_analysis_request,
)
from ...models import JobStatus
from ...analysis_specs import AnalysisSpecRegistry, canonical_input_role


class DatasetLoadError(ValueError):
    """Loaded validation inputs do not match the ownership-bound state refs."""


class ExecutionRejected(ValueError):
    """Defensive execution checks rejected inputs changed after confirmation."""


def analysis_node(
    dataset_loader: DatasetLoader,
    job_submitter: JobSubmitter,
    response_model: MainDecisionModel | None = None,
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
        if not state.dataset_profiles:
            text = _missing_inputs_message(state)
            pending = PendingAnalysisClarification(
                analysis_type=(
                    decision.analysis_type
                    or infer_analysis_type(state.user_message, [])
                ),
                question=text,
                missing=["dataset"],
                source_message=state.user_message,
                source_action=decision.action,
                proposal=_analysis_proposal(state),
            )
            return Command(
                update={
                    "pending_analysis": pending,
                    "response_text": text,
                    "response_blocks": [text_block(text)],
                    "outcome": "needs_input",
                    "failure_code": "missing_analysis_parameter",
                    "step_budget": next_budget,
                },
                goto=END,
            )
        try:
            dataset_refs = _load_validation_refs(state, dataset_loader)
        except DatasetLoadError as exc:
            text = _localized_error(
                state,
                f"当前分析输入无法通过校验：{str(exc)[:500]}",
                f"I could not validate the current analysis inputs: {str(exc)[:500]}",
            )
            return Command(
                update={
                    "response_text": text,
                    "response_blocks": [text_block(text)],
                    "outcome": "failed",
                    "failure_code": "dataset_validation_failed",
                    "step_budget": next_budget,
                },
                goto=END,
            )
        except Exception as exc:
            text = _localized_error(
                state,
                f"分析输入无法加载：{type(exc).__name__}: {str(exc)[:400]}",
                f"The analysis inputs could not be loaded: {type(exc).__name__}: {str(exc)[:400]}",
            )
            return Command(
                update={
                    "response_text": text,
                    "response_blocks": [text_block(text)],
                    "outcome": "failed",
                    "failure_code": "dataset_loader_failed",
                    "step_budget": next_budget,
                },
                goto=END,
            )
        request_text = _analysis_request_text(state)
        proposal = _analysis_proposal(state)
        # Keep the model as a proposal source, but make the module selection
        # deterministic when it omitted the type.  This is what lets a Chinese
        # request such as “推断调控网络” enter GMA without another dead-end
        # clarification turn.
        if proposal.analysis_type is None:
            inferred_type = infer_analysis_type(
                request_text,
                [
                    canonical_input_role(
                        getattr(item.profile, "role", None)
                        or getattr(item, "role", "")
                    )
                    for item in state.dataset_profiles
                ],
            )
            if inferred_type is not None:
                proposal = proposal.model_copy(update={"analysis_type": inferred_type})
        resolved = resolve_analysis_request(
            request_text,
            [item.profile for item in state.dataset_profiles],
            llm_proposal=proposal,
            prior_params=(
                state.pending_plan.params
                if state.pending_plan is not None
                else state.confirmed_params
            ),
        )
        if resolved.analysis_type is not None:
            present_roles = {
                canonical_input_role(
                    getattr(item.profile, "role", None)
                    or getattr(item, "role", "")
                )
                for item in state.dataset_profiles
            }
            missing_roles = sorted(
                set(AnalysisSpecRegistry().required_roles(str(resolved.analysis_type).lower()))
                - present_roles
            )
            if missing_roles:
                text = (
                    f"当前输入不支持 {resolved.analysis_type}。缺少必要的数据角色：{', '.join(missing_roles)}。"
                    if _is_chinese(state.user_message)
                    else f"The platform does not support {resolved.analysis_type} with the current inputs. "
                    f"Missing required input roles: {', '.join(missing_roles)}."
                )
                return Command(
                    update={
                        "resolved_request": resolved,
                        "response_text": text,
                        "response_blocks": [text_block(text)],
                        "outcome": "unsupported",
                        "failure_code": "unsupported_request",
                        "step_budget": next_budget,
                    },
                    goto=END,
                )
        try:
            report = validate_analysis_request(resolved, dataset_refs)
        except Exception as exc:
            text = _localized_error(
                state,
                f"分析输入未通过确定性校验：{type(exc).__name__}: {str(exc)[:400]}",
                f"The analysis inputs failed deterministic validation: {type(exc).__name__}: {str(exc)[:400]}",
            )
            return Command(
                update={
                    "resolved_request": resolved,
                    "response_text": text,
                    "response_blocks": [text_block(text)],
                    "outcome": "failed",
                    "failure_code": "validation_failed",
                    "step_budget": next_budget,
                },
                goto=END,
            )
        if report.ok:
            if resolved.analysis_type is None or resolved.params is None:
                raise RuntimeError("successful validation must contain resolved parameters")
            sample_scope = _sample_scope_from_inputs(report, resolved, dataset_refs)
            existing_pending = state.pending_analysis
            if existing_pending is None or existing_pending.status != "consumed":
                pending = _candidate_pending(
                    state,
                    resolved,
                    report,
                    sample_scope,
                )
                pending = _with_model_confirmation(
                    state,
                    pending,
                    response_model,
                )
                if response_model is not None and hasattr(response_model, "last_assistant_message"):
                    next_budget = next_budget.model_copy(update={
                        "used_model_steps": min(
                            next_budget.max_model_steps,
                            next_budget.used_model_steps + 1,
                        ),
                    })
                return Command(
                    update={
                        "resolved_request": resolved,
                        "validation_report": report,
                        "pending_analysis": pending,
                        "pending_interrupt": None,
                        "response_text": pending.question,
                        "response_blocks": [text_block(pending.question)],
                        "outcome": "needs_input",
                        "failure_code": "missing_analysis_parameter",
                        "step_budget": next_budget,
                    },
                    goto=END,
                )
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
                    "outcome": None,
                    "failure_code": None,
                    "step_budget": next_budget,
                },
                goto="analysis",
            )

        pending = PendingAnalysisClarification(
            analysis_type=resolved.analysis_type,
            question=_clarification_question(
                resolved,
                report,
                user_message=state.user_message,
            ),
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
                "outcome": "needs_input" if resolved.missing else "failed",
                "failure_code": (
                    "missing_analysis_parameter"
                    if resolved.missing
                    else _validation_failure_code(report)
                ),
                "step_budget": next_budget,
            },
            goto=END,
        )

    return run


def _validation_failure_code(report: ValidationReport) -> str:
    codes = {item.code for item in report.blocking}
    if "checksum_mismatch" in codes:
        return "checksum_mismatch"
    if "ownership_mismatch" in codes:
        return "ownership_validation_failed"
    return "unsupported_request"


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
                "outcome": "completed",
                "failure_code": None,
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
            goto="analysis_agent",
        )

    if state.step_budget.used_model_steps >= state.step_budget.max_model_steps:
        return Command(
            update={
                "pending_interrupt": None,
                "response_text": "Analysis was not submitted because the step budget was exhausted.",
                "outcome": "needs_input",
                "failure_code": "missing_analysis_parameter",
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
                "outcome": "failed",
                "failure_code": "checksum_mismatch",
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
            "outcome": "completed",
            "failure_code": None,
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


def _is_chinese(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in str(text or ""))


def _localized_error(state: GraphState, chinese: str, english: str) -> str:
    return chinese if _is_chinese(state.user_message) else english


def _missing_inputs_message(state: GraphState) -> str:
    """Explain the next upload in the same language as the user.

    The old fixed English sentence made a capability question look like a
    validation failure.  Keep the response useful even before a bundle exists:
    name all three modules and their required input roles.
    """

    analysis_type = (
        state.decision.analysis_type
        if state.decision is not None
        else None
    ) or infer_analysis_type(state.user_message, [])
    identified = (
        f"我识别为 {analysis_type}（{_analysis_label(analysis_type)}）。"
        if analysis_type and _is_chinese(state.user_message)
        else (
            f"I identified {analysis_type} ({_analysis_label(analysis_type, chinese=False)}). "
            if analysis_type else ""
        )
    )
    if _is_chinese(state.user_message):
        return (
            f"{identified or '我先根据你的请求定位分析模块，但目前还不能唯一确定模块。'}"
            "当前还没有可用的输入文件，"
            "所以暂时不能计算样本数或生成可提交计划。\n"
            "可选模块：DEG（差异基因：counts + metadata）、"
            "DEM（差异代谢物：metabolome + metadata）、"
            "GMA（基因-代谢调控网络：transcriptome + metabolome + group）。\n"
            "请上传对应文件；上传后我会先列出推断的分析类型、数据文件、比较组、"
            "样本计数和默认参数，再请你确认或修改。"
        )
    return (
        f"{identified}I can identify the analysis module first, but no input bundle is available yet, "
        "so sample counts and a runnable plan cannot be computed.\n"
        "Available modules: DEG (counts + metadata), DEM (metabolome + metadata), "
        "and GMA (transcriptome + metabolome + group). Upload the matching files and "
        "I will show the inferred module, files, contrast, sample counts, and defaults "
        "before asking for confirmation."
    )


def _build_pending_plan(
    state: GraphState,
    resolved: ResolvedRequest,
    report: ValidationReport,
) -> PendingPlan:
    if resolved.analysis_type is None or resolved.params is None:
        raise ValueError("a pending plan requires resolved analysis parameters")
    contrast = getattr(resolved.params, "contrast", None)
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
        scope=contrast.scope if contrast is not None else None,
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
    *,
    user_message: str = "",
) -> str:
    if not _is_chinese(user_message):
        if resolved.missing:
            fields = ", ".join(item.field for item in resolved.missing[:3])
            options = "; ".join(
                f"{item.field}: {', '.join(item.options[:20])}"
                for item in resolved.missing[:3]
                if item.options
            )
            suffix = f" Options: {options}." if options else ""
            return f"I need {fields} before I can build the analysis plan.{suffix}"[:1200]
        details = "; ".join(item.message[:500] for item in report.blocking[:3])
        return f"The analysis request did not pass validation: {details}"[:1200]
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


def _sample_scope_from_report(report: ValidationReport) -> list[StratumSummary]:
    previews = report.previews or ([report.preview] if report.preview is not None else [])
    return [StratumSummary(
        stratum=dict(preview.same_values),
        tested_count=preview.tested_count,
        reference_count=preview.reference_count,
        included=True,
    ) for preview in previews if preview is not None]


def _sample_scope_from_inputs(
    report: ValidationReport,
    resolved: ResolvedRequest,
    dataset_refs: list[DatasetRef],
) -> list[StratumSummary]:
    """Derive display counts from the matrix/metadata intersection."""

    params = resolved.params
    contrast = getattr(params, "contrast", None) if params is not None else None
    if contrast is None:
        return _sample_scope_from_report(report)
    metadata_ref = next((item for item in dataset_refs if item.role == "metadata"), None)
    matrix_role = "counts" if resolved.analysis_type == "DEG" else "metabolome"
    matrix_ref = next((item for item in dataset_refs if item.role == matrix_role), None)
    if metadata_ref is None or matrix_ref is None:
        return _sample_scope_from_report(report)
    metadata_text = metadata_ref.content.decode("utf-8-sig", errors="replace")
    rows = list(csv.DictReader(io.StringIO(metadata_text, newline="")))
    matrix_header = next(csv.reader(io.StringIO(matrix_ref.content.decode("utf-8-sig", errors="replace"))), [])
    matrix_samples = {str(value).strip() for value in matrix_header[1:] if str(value).strip()}
    sample_field = str((rows[0] if rows else {}).get("sample_id", ""))
    if not sample_field:
        sample_field = str(next(iter(rows[0]), "")) if rows else ""
    rows = [
        {str(key).strip(): str(value or "").strip() for key, value in row.items() if key is not None}
        for row in rows
        if str(row.get(sample_field, "")).strip() in matrix_samples
    ]
    fields = list(contrast.scope.blocking_fields) if contrast.scope.mode == "stratified" else []
    groups: dict[tuple[str, ...], list[dict[str, str]]] = {}
    for row in rows:
        key = tuple(row.get(field, "") for field in fields)
        groups.setdefault(key, []).append(row)
    min_replicates = int(getattr(params, "min_replicates", 2))
    result: list[StratumSummary] = []
    for key, group in groups.items():
        tested_count = sum(row.get(contrast.compare_field, "") == contrast.tested_level for row in group)
        reference_count = sum(row.get(contrast.compare_field, "") == contrast.reference_level for row in group)
        included = tested_count >= min_replicates and reference_count >= min_replicates
        result.append(StratumSummary(
            stratum=dict(zip(fields, key)),
            tested_count=tested_count,
            reference_count=reference_count,
            included=included,
            exclusion_reason=None if included else f"requires at least {min_replicates} samples per group",
        ))
    return result or _sample_scope_from_report(report)


def _candidate_pending(
    state: GraphState,
    resolved: ResolvedRequest,
    report: ValidationReport,
    sample_scope: list[StratumSummary],
) -> PendingAnalysisClarification:
    proposal = _analysis_proposal(state)
    if proposal.analysis_type is None and resolved.analysis_type is not None:
        proposal = proposal.model_copy(update={"analysis_type": resolved.analysis_type})
    if resolved.params is not None:
        contrast = getattr(resolved.params, "contrast", None)
        proposal = AnalysisProposal(
            analysis_type=resolved.params.analysis_type,
            compare_field=getattr(contrast, "compare_field", None),
            tested_level=getattr(contrast, "tested_level", None),
            reference_level=getattr(contrast, "reference_level", None),
            scope=(getattr(contrast, "scope", None) or ScopeSpec(mode="unknown")),
            requested_params=resolved.params.legacy_params(),
        )
    return PendingAnalysisClarification(
        analysis_type=resolved.analysis_type,
        question=_candidate_confirmation_question(state, resolved, report, sample_scope),
        missing=[],
        options=[],
        source_message=state.user_message,
        input_bundle_id=state.active_input_bundle_id,
        source_action=state.decision.action if state.decision is not None else "propose_plan",
        proposal=proposal,
        sample_scope=sample_scope,
        candidate_validation=report,
    )


def _candidate_confirmation_question(
    state: GraphState,
    resolved: ResolvedRequest,
    report: ValidationReport,
    sample_scope: list[StratumSummary],
) -> str:
    params = resolved.params
    if params is None or not hasattr(params, "contrast"):
        inputs = _input_summary(state)
        values = _parameter_summary(params) if params is not None else ""
        if _is_chinese(state.user_message):
            return (
                f"我根据当前文件推断为 {getattr(params, 'analysis_type', resolved.analysis_type)}"
                f"（{_analysis_label(getattr(params, 'analysis_type', resolved.analysis_type))}）。\n"
                f"数据：{inputs}。\n"
                f"参数：{values or '使用模块默认值'}。\n"
                "请确认这份分析计划，或告诉我需要修改的参数。"
            )[:1200]
        return (
            f"I inferred {getattr(params, 'analysis_type', resolved.analysis_type)} "
            f"({_analysis_label(getattr(params, 'analysis_type', resolved.analysis_type), chinese=False)}). "
            f"Inputs: {inputs}. Parameters: {values or 'module defaults'}. "
            "Please confirm this analysis plan or tell me what to change."
        )[:1200]
    contrast = params.contrast
    scope = contrast.scope
    chinese = _is_chinese(state.user_message)
    scope_text = (
        ("全部样本" if chinese else "all samples")
        if scope.mode == "all"
        else (("按 " + ", ".join(scope.blocking_fields) + " 分层") if chinese else ("stratified by " + ", ".join(scope.blocking_fields)))
        if scope.mode == "stratified"
        else (("固定条件 " + ", ".join(f"{k}={v}" for k, v in scope.fixed_filters.items())) if chinese else ("fixed filters " + ", ".join(f"{k}={v}" for k, v in scope.fixed_filters.items())))
    )
    counts = "; ".join(
        f"{', '.join(f'{k}={v}' for k, v in item.stratum.items()) or ('全部' if chinese else 'all')}: "
        + (
            f"测试组={item.tested_count}，参考组={item.reference_count}"
            if chinese
            else f"tested={item.tested_count}, reference={item.reference_count}"
        )
        for item in sample_scope
    ) or ("暂无分层样本计数" if chinese else "No stratum count preview is available.")
    inputs = _input_summary(state)
    parameters = _parameter_summary(params)
    if chinese:
        return (
            f"我根据当前文件推断为 {params.analysis_type}（{_analysis_label(params.analysis_type)}）。\n"
            f"数据：{inputs}。\n"
            f"设置：比较字段={contrast.compare_field}，测试组={contrast.tested_level}，"
            f"参考组={contrast.reference_level}，范围={scope_text}。\n"
            f"样本计数：{counts}。\n"
            f"其他参数：{parameters or '使用模块默认值'}。\n"
            "请确认这份分析计划，或直接告诉我需要修改的字段、比较组、范围或参数。"
        )[:1200]
    return (
        f"I inferred {params.analysis_type} ({_analysis_label(params.analysis_type, chinese=False)}) from the current files. "
        f"Inputs: {inputs}. Compare "
        f"{contrast.compare_field}, testing {contrast.tested_level} against "
        f"{contrast.reference_level}, using {scope_text}. Sample counts: {counts}. "
        f"Other parameters: {parameters or 'module defaults'}. "
        "Please confirm this analysis plan or tell me what to change."
    )[:1200]


def _input_summary(state: GraphState) -> str:
    items: list[str] = []
    for item in state.dataset_profiles:
        profile = getattr(item, "profile", item)
        role = canonical_input_role(
            getattr(profile, "role", None) or getattr(item, "role", "")
        )
        filename = str(getattr(item, "filename", "") or role)
        items.append(f"{role}={filename}")
    return ", ".join(items) or "none"


def _parameter_summary(params: object) -> str:
    dump = getattr(params, "model_dump", None)
    if not callable(dump):
        return ""
    values = dump(mode="python", exclude={"analysis_type", "contrast"})
    # Keep the public plan language domain friendly and avoid exposing the
    # internal field spelling in the chat transcript.
    labels = {"min_replicates": "min_samples_per_group"}
    return ", ".join(
        f"{labels.get(key, key)}={value}" for key, value in values.items()
    )


def _analysis_label(analysis_type: object, *, chinese: bool = True) -> str:
    labels = {
        "DEG": ("差异基因分析", "differential gene analysis"),
        "DEM": ("差异代谢物分析", "differential metabolite analysis"),
        "GMA": ("基因-代谢调控网络分析", "gene-metabolite network analysis"),
    }
    pair = labels.get(str(analysis_type))
    return (pair[0] if chinese else pair[1]) if pair else str(analysis_type)


def _with_model_confirmation(
    state: GraphState,
    pending: PendingAnalysisClarification,
    response_model: MainDecisionModel | None,
) -> PendingAnalysisClarification:
    """Let the role model phrase validated candidate facts; fallback stays deterministic."""

    if response_model is None:
        return pending
    if not hasattr(response_model, "last_assistant_message"):
        # Recorded/test models and simple injected fixtures do not implement
        # the live phrasing boundary; keep the deterministic fallback.
        return pending
    from ..context import ContextAssembler

    prompt_state = state.model_copy(update={
        "pending_analysis": pending,
        "validation_report": pending.candidate_validation,
    })
    try:
        context = ContextAssembler().assemble_for_analysis(prompt_state)
        raw = response_model(context, role=AgentRole.ANALYSIS)
        output = AnalysisModelOutput.model_validate(raw)
        text = output.answer if output.decision.action == "answer" else output.decision.question
        if text and _grounded_confirmation_text(text, pending):
            return pending.model_copy(update={"question": text[:1200]})
    except Exception:
        pass
    return pending


def _grounded_confirmation_text(text: str, pending: PendingAnalysisClarification) -> bool:
    """Accept model phrasing only when it repeats the validated candidate facts."""

    normalized = text.casefold()
    if normalized.strip() in {"ask_user", "tool_call", "reroute"}:
        return False
    proposal = pending.proposal
    if proposal is None:
        return False
    required = [proposal.analysis_type, proposal.compare_field, proposal.tested_level, proposal.reference_level]
    if any(value and str(value).casefold() not in normalized for value in required):
        return False
    for item in pending.sample_scope:
        for count in (item.tested_count, item.reference_count):
            if str(count) not in normalized:
                return False
    # A role model can be instructed to mirror the user language, but the
    # deterministic fallback must remain authoritative when it does not.
    if _is_chinese(pending.source_message) != _is_chinese(text):
        return False
    return True


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
