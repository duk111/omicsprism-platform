from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from pathlib import Path
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from fastapi import HTTPException, UploadFile

from ..analysis_specs import AnalysisSpecRegistry
from ..models import AnalysisType, JobOwnerType, JobRecord, JobStatus, UploadedFileInfo
from ..preflight import PreflightService, build_contrast_preview
from .dataset_profile import DatasetProfile, build_dataset_profiles
from .schemas import ToolName, ToolResult
from .approvals import ApprovalGate
from .plans import PlanNotFound, PlanStore, compute_plan_hash
from .policy import PolicyGuard
from .product_store import AgentProductStore, AgentResourceNotFound
from .schemas import (
    ActiveProfile,
    AgentInputBundleStatus,
    AgentInputFileRecord,
    AgentInputSourceKind,
    AgentInputSourceRef,
)


MAX_TOOL_ROWS = 50
MAX_TOOL_BYTES = 32 * 1024

_FIGURE_JSON_BY_ANALYSIS = {
    AnalysisType.DIFFERENTIAL: frozenset({"volcano.json"}),
    AnalysisType.DEM: frozenset({"volcano.json"}),
    AnalysisType.CORRELATION: frozenset({
        "pca.json",
        "dendrogram.json",
        "upset.json",
        "bubble-heatmap.json",
        "scatter-panels.json",
        "violin-box.json",
        "corr-heatmap.json",
        "ridge.json",
        "line-panels.json",
        "circos.json",
    }),
}
_JSON_PATH_PART = re.compile(r"^[A-Za-z0-9_-]+$")
_JSON_ENTITY_FIELDS = (
    "id", "sample_id", "entity_id", "feature", "gene", "metabolite",
    "node_id", "label", "name", "text",
)


class ToolConfigurationError(RuntimeError):
    """工具缺少受控运行时依赖，不能执行。"""


@dataclass(frozen=True)
class AgentInputFile:
    filename: str
    content: bytes


class AgentJobStore(Protocol):
    def get_for_user(self, job_id: str, user_id: str) -> JobRecord:
        ...

    def save(self, job: JobRecord) -> None:
        ...


class AgentFileService(Protocol):
    def copy_input_artifact(self, source_job_id: str, target_job_id: str, source):
        ...

    def recent_log(self, job_id: str):
        ...

    def read_artifact_text(self, job_id: str, relative_path: str, *, max_chars: int | None = None) -> str:
        ...

    def open_artifact(self, job_id: str, relative_path: str):
        ...


class AgentExecutor(Protocol):
    def enqueue(self, job_id: str) -> None:
        ...


class AgentInputSource(Protocol):
    """已经绑定会话用户的输入来源；模型永远拿不到该对象。"""

    user_id: str
    ref: AgentInputSourceRef
    project_id: str | None
    project_name: str
    owner_label: str | None

    def load_inputs(self) -> Mapping[str, AgentInputFile]:
        ...

    def copy_inputs(self, target_job_id: str) -> list[UploadedFileInfo]:
        ...


class AgentStagedFileService(Protocol):
    def open_storage_key(self, storage_key: str):
        ...

    def copy_staged_input(self, target_job_id: str, item: AgentInputFileRecord) -> UploadedFileInfo:
        ...


class ExistingJobInputSource:
    def __init__(self, *, user_id: str, source_job_id: str,
                 job_store: AgentJobStore, files: AgentFileService) -> None:
        self.user_id = user_id
        self.job_store = job_store
        self.files = files
        self.job = job_store.get_for_user(source_job_id, user_id)
        self.ref = AgentInputSourceRef(kind=AgentInputSourceKind.EXISTING_JOB, source_id=source_job_id)
        self.project_id = self.job.project_id
        self.project_name = self.job.project_name
        self.owner_label = self.job.owner_label

    def load_inputs(self) -> Mapping[str, AgentInputFile]:
        inputs: dict[str, AgentInputFile] = {}
        for item in self.job.inputs:
            with self.files.open_artifact(self.job.id, item.path) as handle:
                content = handle.read(50 * 1024 * 1024 + 1)
            if len(content) > 50 * 1024 * 1024:
                raise ToolConfigurationError(f"input {item.field} exceeds 50 MB")
            _verify_input_checksum(content, item.checksum, item.field)
            inputs[item.field] = AgentInputFile(item.filename, bytes(content))
        return inputs

    def copy_inputs(self, target_job_id: str) -> list[UploadedFileInfo]:
        return [
            self.files.copy_input_artifact(self.job.id, target_job_id, item)
            for item in self.job.inputs
        ]


class StagedBundleInputSource:
    def __init__(self, *, user_id: str, thread_id: str, bundle_id: str,
                 product_store: AgentProductStore, files: AgentStagedFileService,
                 now: datetime | None = None) -> None:
        current = now or datetime.now(timezone.utc)
        bundle = product_store.get_input_bundle(bundle_id=bundle_id, user_id=user_id)
        if bundle.thread_id != thread_id:
            raise AgentResourceNotFound(bundle_id)
        if bundle.status is not AgentInputBundleStatus.ACTIVE or current >= bundle.expires_at:
            raise ToolConfigurationError("staged input bundle is not active")
        self.user_id = user_id
        self.product_store = product_store
        self.files = files
        self.items = product_store.list_input_files(bundle_id=bundle_id, user_id=user_id)
        self.ref = AgentInputSourceRef(kind=AgentInputSourceKind.STAGED_BUNDLE, source_id=bundle_id)
        self.project_id = None
        self.project_name = "Copilot analysis"
        self.owner_label = None

    def load_inputs(self) -> Mapping[str, AgentInputFile]:
        inputs: dict[str, AgentInputFile] = {}
        for item in self.items:
            with self.files.open_storage_key(item.storage_key) as handle:
                content = handle.read(50 * 1024 * 1024 + 1)
            if len(content) > 50 * 1024 * 1024:
                raise ToolConfigurationError(f"input {item.field} exceeds 50 MB")
            _verify_input_checksum(content, item.checksum, item.field)
            inputs[item.field] = AgentInputFile(item.filename, bytes(content))
        return inputs

    def copy_inputs(self, target_job_id: str) -> list[UploadedFileInfo]:
        return [self.files.copy_staged_input(target_job_id, item) for item in self.items]


@dataclass
class AgentToolRuntime:
    """由 API 会话创建的工具运行时；身份和服务句柄不会进入模型上下文。"""

    user_id: str
    inputs: Mapping[str, AgentInputFile] = field(default_factory=dict)
    input_source: AgentInputSource | None = None
    input_source_job_id: str | None = None
    analysis_specs: AnalysisSpecRegistry | None = None
    preflight_service: PreflightService | None = None
    plans: PlanStore | None = None
    job_store: AgentJobStore | None = None
    files: AgentFileService | None = None
    executor: AgentExecutor | None = None
    approval_gate: ApprovalGate | None = None

    @classmethod
    def from_source_job(
        cls,
        *,
        user_id: str,
        source_job_id: str,
        job_store: AgentJobStore,
        files: AgentFileService,
        plans: PlanStore | None = None,
        executor: AgentExecutor | None = None,
        approval_gate: ApprovalGate | None = None,
    ) -> "AgentToolRuntime":
        source = ExistingJobInputSource(
            user_id=user_id,
            source_job_id=source_job_id,
            job_store=job_store,
            files=files,
        )
        return cls.from_input_source(
            user_id=user_id,
            input_source=source,
            plans=plans,
            job_store=job_store,
            files=files,
            executor=executor,
            approval_gate=approval_gate,
        )

    @classmethod
    def from_input_source(
        cls,
        *,
        user_id: str,
        input_source: AgentInputSource,
        plans: PlanStore | None = None,
        job_store: AgentJobStore | None = None,
        files: AgentFileService | None = None,
        executor: AgentExecutor | None = None,
        approval_gate: ApprovalGate | None = None,
    ) -> "AgentToolRuntime":
        if input_source.user_id != user_id:
            raise ToolConfigurationError("input source belongs to another user")
        return cls(
            user_id=user_id,
            inputs=input_source.load_inputs(),
            input_source=input_source,
            input_source_job_id=(
                input_source.ref.source_id
                if input_source.ref.kind is AgentInputSourceKind.EXISTING_JOB
                else None
            ),
            plans=plans,
            job_store=job_store,
            files=files,
            executor=executor,
            approval_gate=approval_gate,
        )

    def __post_init__(self) -> None:
        if not self.user_id:
            raise ToolConfigurationError("session user_id is required")
        if self.input_source is not None and self.input_source.user_id != self.user_id:
            raise ToolConfigurationError("input source belongs to another user")
        if self.analysis_specs is None:
            self.analysis_specs = AnalysisSpecRegistry()
        if self.preflight_service is None:
            self.preflight_service = PreflightService()

    @property
    def input_source_ref(self) -> AgentInputSourceRef:
        if self.input_source is not None:
            return self.input_source.ref
        if self.input_source_job_id:
            return AgentInputSourceRef(
                kind=AgentInputSourceKind.EXISTING_JOB,
                source_id=self.input_source_job_id,
            )
        raise ToolConfigurationError("agent input source is not configured")

    def inspect_uploaded_inputs(self) -> ToolResult:
        rows = [_inspect_input(field, item) for field, item in sorted(self.inputs.items())]
        return _tool_result(ToolName.INSPECT_UPLOADED_INPUTS, rows=rows)

    def inspect_dataset(self) -> list[DatasetProfile]:
        """Return the same inspection facts as typed DatasetProfile models."""
        return build_dataset_profiles({
            field: (item.filename, item.content)
            for field, item in sorted(self.inputs.items())
        })

    def get_analysis_spec(self, analysis_type: AnalysisType | str) -> ToolResult:
        assert self.analysis_specs is not None
        spec = self.analysis_specs.get(analysis_type)
        return _tool_result(ToolName.GET_ANALYSIS_SPEC, rows=[{
            "analysis_type": spec.analysis_type.value,
            "display_label": spec.display_label,
            "inputs": [{"name": rule.name, "required": rule.required} for rule in spec.input_rules],
            "parameters": [
                {"name": rule.name, "required": rule.required, "default": rule.default}
                for rule in spec.parameter_rules
            ],
        }])

    def run_preflight(self, analysis_type: AnalysisType | str, params: Mapping[str, Any]) -> ToolResult:
        atype = AnalysisType(analysis_type)
        files = _upload_files(self.inputs)
        assert self.preflight_service is not None
        assert self.analysis_specs is not None
        requested_params = self.analysis_specs.requested_params(atype, dict(params))
        effective_params = self.analysis_specs.effective_params(atype, requested_params)
        response = self.preflight_service.preflight(atype, params=effective_params, files=files)
        contrasts: list[dict[str, object]] = []
        contrast_issues = []
        if atype in {AnalysisType.DIFFERENTIAL, AnalysisType.DEM}:
            metadata = self.inputs.get("metadata")
            metadata_rows = _dict_rows(metadata.content) if metadata is not None else []
            preview, contrast_issues = build_contrast_preview(metadata_rows, effective_params)
            contrasts = [item.as_dict() for item in preview]

        errors = [item.model_dump(mode="json") for item in response.errors]
        errors.extend(item.model_dump(mode="json") for item in contrast_issues if item.severity == "error")
        warnings = [item.model_dump(mode="json") for item in response.warnings]
        warnings.extend(item.model_dump(mode="json") for item in contrast_issues if item.severity == "warning")
        can_submit = response.can_submit and not errors
        if atype in {AnalysisType.DIFFERENTIAL, AnalysisType.DEM}:
            can_submit = can_submit and bool(contrasts)
        row = {
            "analysis_type": atype.value,
            "can_submit": can_submit,
            "requested_params": requested_params,
            "effective_params": effective_params,
            "contrasts": contrasts,
            # 完整 feature/sample ID 列表在真实组学矩阵中可达数万项，不能进入
            # 32 KB 工具结果。这里只保留诊断所需的有界摘要和总数。
            "files": [_bounded_preflight_file(item) for item in response.files],
            "errors": errors,
            "warnings": warnings,
            "alignment": _alignment_diagnostic(atype, self.inputs),
        }
        return _tool_result(
            ToolName.RUN_PREFLIGHT,
            rows=[row],
            ok=can_submit,
            error_code=None if can_submit else "preflight_blocked",
        )

    def submit_approved_plan(self, plan_id: str, idempotency_key: str) -> ToolResult:
        if not all((self.plans, self.job_store, self.files, self.executor, self.approval_gate)):
            raise ToolConfigurationError("submission services are not configured")
        if not idempotency_key.strip():
            return _tool_result(ToolName.SUBMIT_APPROVED_PLAN, rows=[], ok=False, error_code="invalid_idempotency_key")
        try:
            plan = self.plans.get(plan_id=plan_id, user_id=self.user_id)
        except PlanNotFound:
            return _tool_result(ToolName.SUBMIT_APPROVED_PLAN, rows=[], ok=False, error_code="not_found")
        if plan.submitted_job_ids:
            return _tool_result(ToolName.SUBMIT_APPROVED_PLAN, rows=[{"job_ids": list(plan.submitted_job_ids), "idempotent": True}])
        # correlation 不产生 contrasts，其可提交性由 run_preflight 的 can_submit 保证。
        if plan.analysis_type in {AnalysisType.DIFFERENTIAL, AnalysisType.DEM} and not plan.contrasts:
            return _tool_result(ToolName.SUBMIT_APPROVED_PLAN, rows=[], ok=False, error_code="preflight_blocked")
        if compute_plan_hash(plan) != plan.plan_hash:
            return _tool_result(ToolName.SUBMIT_APPROVED_PLAN, rows=[], ok=False, error_code="plan_hash_mismatch")
        try:
            runtime_ref = self.input_source_ref
        except ToolConfigurationError:
            runtime_ref = None
        if runtime_ref != plan.input_source:
            return _tool_result(ToolName.SUBMIT_APPROVED_PLAN, rows=[], ok=False, error_code="input_source_mismatch")
        fresh_preflight = self.run_preflight(plan.analysis_type, plan.effective_params)
        if not fresh_preflight.ok:
            return _tool_result(ToolName.SUBMIT_APPROVED_PLAN, rows=[], ok=False, error_code="preflight_blocked")
        if fresh_preflight.rows[0].get("contrasts") != plan.contrasts:
            return _tool_result(ToolName.SUBMIT_APPROVED_PLAN, rows=[], ok=False, error_code="preflight_changed")
        if not plan.approval_id or not self.approval_gate.is_valid(
            approval_id=plan.approval_id,
            run_id=plan.run_id,
            user_id=self.user_id,
            plan_hash=plan.plan_hash,
            now=datetime.now(timezone.utc),
        ):
            return _tool_result(ToolName.SUBMIT_APPROVED_PLAN, rows=[], ok=False, error_code="approval_required")
        job_id = str(uuid5(NAMESPACE_URL, f"omicsprism:{self.user_id}:{idempotency_key}"))
        try:
            existing = self.job_store.get_for_user(job_id, self.user_id)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            existing = None
        except LookupError:
            existing = None
        if existing is None:
            now = datetime.now(timezone.utc)
            if self.input_source is not None:
                inputs = self.input_source.copy_inputs(job_id)
                project_id = self.input_source.project_id or job_id
                project_name = self.input_source.project_name
                owner_label = self.input_source.owner_label
            else:
                source = self.job_store.get_for_user(plan.input_source.source_id, self.user_id)
                inputs = [self.files.copy_input_artifact(source.id, job_id, item) for item in source.inputs]
                project_id = source.project_id or job_id
                project_name = source.project_name
                owner_label = source.owner_label
            job = JobRecord(
                id=job_id,
                project_id=project_id,
                project_name=project_name,
                analysis_type=plan.analysis_type,
                status=JobStatus.QUEUED,
                created_at=now,
                updated_at=now,
                owner_type=JobOwnerType.USER,
                owner_id=self.user_id,
                owner_label=owner_label,
                inputs=inputs,
                params=plan.effective_params,
                progress=0,
                progress_step="Queued",
            )
            self.job_store.save(job)
            self.executor.enqueue(job_id)
        plan.submitted_job_ids = [job_id]
        plan.idempotency_key = idempotency_key
        self.plans.save(plan)
        return _tool_result(ToolName.SUBMIT_APPROVED_PLAN, rows=[{"job_ids": [job_id], "idempotent": existing is not None}])

    def get_jobs_status(self, job_ids: Sequence[str]) -> ToolResult:
        if self.job_store is None:
            raise ToolConfigurationError("job services are not configured")
        rows: list[dict[str, Any]] = []
        for job_id in list(dict.fromkeys(job_ids))[:MAX_TOOL_ROWS]:
            try:
                job = self.job_store.get_for_user(job_id, self.user_id)
            except LookupError:
                return _tool_result(ToolName.GET_JOBS_STATUS, rows=[], ok=False, error_code="not_found")
            log_name = log_excerpt = None
            if self.files is not None and hasattr(self.files, "recent_log"):
                log_name, log_excerpt = self.files.recent_log(job.id)
            rows.append({
                "job_id": job.id,
                "status": job.status.value,
                "progress": job.progress,
                "progress_step": job.progress_step,
                "error": job.error,
                "log_name": log_name,
                "log_excerpt": log_excerpt,
                "artifacts": sorted({
                    str(name)
                    for item in [*job.artifacts, *job.result_files]
                    for name in (getattr(item, "path", None), getattr(item, "filename", None))
                    if name and _allowed_result_artifact(str(name), job.analysis_type)
                })[:20],
            })
        return _tool_result(ToolName.GET_JOBS_STATUS, rows=rows)

    def query_result_evidence(
        self,
        job_id: str,
        artifact: str,
        filters: Mapping[str, Any] | None = None,
        field_path: str | None = None,
        sort: str | None = None,
        limit: int | None = None,
        resolve_entity: str | None = None,
    ) -> ToolResult:
        if self.job_store is None or self.files is None:
            raise ToolConfigurationError("result services are not configured")
        if not _allowed_result_artifact(artifact, job_type=None):
            return _tool_result(ToolName.QUERY_RESULT_EVIDENCE, rows=[], ok=False, error_code="artifact_not_allowed")
        job = self.job_store.get_for_user(job_id, self.user_id)
        if not _allowed_result_artifact(artifact, job_type=job.analysis_type):
            return _tool_result(ToolName.QUERY_RESULT_EVIDENCE, rows=[], ok=False, error_code="artifact_not_allowed")
        artifact_info = next(
            (item for item in [*job.artifacts, *job.result_files]
             if item.path == artifact or item.filename == artifact or getattr(item, "name", None) == artifact),
            None,
        )
        if artifact_info is None:
            return _tool_result(ToolName.QUERY_RESULT_EVIDENCE, rows=[], ok=False, error_code="not_found")
        text = self.files.read_artifact_text(job.id, artifact_info.path)
        if Path(artifact).suffix.lower() == ".json":
            return _query_json_evidence(
                text=text,
                artifact=artifact_info.path,
                checksum=artifact_info.checksum,
                field_path=field_path,
                filters=filters,
                sort=sort,
                limit=limit,
                resolve_entity=resolve_entity,
            )
        reader = csv.DictReader(io.StringIO(text))
        rows = [
            {"_row_id": row_id, **{str(key): value for key, value in row.items() if key is not None}}
            for row_id, row in enumerate(reader, start=1)
        ]
        effective_filters = dict(filters or {})
        if resolve_entity:
            effective_filters.setdefault("Gene", resolve_entity)
        rows = [row for row in rows if all(str(row.get(key, "")) == str(value) for key, value in effective_filters.items())]
        if sort:
            sort_field, _, direction = sort.partition(" ")
            reverse = direction.strip().lower() == "desc"
            if not all(sort_field in row for row in rows):
                return _tool_result(ToolName.QUERY_RESULT_EVIDENCE, rows=[], ok=False, error_code="invalid_sort")
            rows.sort(key=lambda row: _sort_value(row.get(sort_field)), reverse=reverse)
        max_rows = min(MAX_TOOL_ROWS, max(1, int(limit or MAX_TOOL_ROWS)))
        selected = rows[:max_rows]
        result = _tool_result(
            ToolName.QUERY_RESULT_EVIDENCE,
            rows=selected,
            artifact=artifact_info.path,
            checksum=artifact_info.checksum or "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest(),
            filters=effective_filters,
            sort=sort,
        )
        result.row_count = len(rows)
        result.truncated = result.truncated or len(selected) < len(rows)
        return result


ToolHandler = Callable[..., ToolResult]


class ToolExecutor(Protocol):
    def execute(self, name: ToolName, **kwargs: Any) -> ToolResult:
        ...


class ToolRegistry:
    def __init__(self, runtime: AgentToolRuntime | None = None) -> None:
        if runtime is None:
            self._handlers = {name: _unconfigured_handler for name in ToolName}
        else:
            self._handlers: dict[ToolName, ToolHandler] = {
                ToolName.INSPECT_UPLOADED_INPUTS: runtime.inspect_uploaded_inputs,
                ToolName.GET_ANALYSIS_SPEC: runtime.get_analysis_spec,
                ToolName.RUN_PREFLIGHT: runtime.run_preflight,
                ToolName.SUBMIT_APPROVED_PLAN: runtime.submit_approved_plan,
                ToolName.GET_JOBS_STATUS: runtime.get_jobs_status,
                ToolName.QUERY_RESULT_EVIDENCE: runtime.query_result_evidence,
            }

    def names(self) -> tuple[ToolName, ...]:
        return tuple(self._handlers)

    def call(self, name: ToolName | str, **kwargs: Any) -> ToolResult:
        return self._handlers[ToolName(name)](**kwargs)


class PolicyToolExecutor:
    """所有真实工具调用的授权入口；身份来自运行时，不来自模型参数。"""

    def __init__(self, registry: ToolRegistry, *, runtime: AgentToolRuntime,
                 active_profile: ActiveProfile, policy: PolicyGuard) -> None:
        self.registry = registry
        self.runtime = runtime
        self.active_profile = active_profile
        self.policy = policy

    def execute(self, name: ToolName | str, **kwargs: Any) -> ToolResult:
        tool = ToolName(name)
        self.policy.authorize(
            user_id=self.runtime.user_id,
            active_profile=self.active_profile,
            tool=tool,
            resource_id=kwargs.get("job_id"),
            approval_id=kwargs.get("approval_id"),
        )
        return self.registry.call(tool, **kwargs)


def _unconfigured_handler(**_kwargs: Any) -> ToolResult:
    raise ToolConfigurationError("agent tool runtime is not configured")


def _upload_files(inputs: Mapping[str, AgentInputFile]) -> dict[str, UploadFile]:
    return {
        field: UploadFile(filename=item.filename, file=io.BytesIO(item.content))
        for field, item in inputs.items()
    }


def _dict_rows(content: bytes) -> list[dict[str, str]]:
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig", errors="replace")))
    return [
        {str(key).strip(): (value or "").strip() for key, value in row.items() if key is not None}
        for row in reader
    ]


def _tool_result(
    tool: ToolName,
    *,
    rows: list[dict[str, Any]],
    ok: bool = True,
    artifact: str | None = None,
    checksum: str | None = None,
    filters: Mapping[str, Any] | None = None,
    sort: str | None = None,
    error_code: str | None = None,
) -> ToolResult:
    trimmed = rows[:MAX_TOOL_ROWS]
    while True:
        result = ToolResult(
            tool=tool,
            ok=ok,
            rows=trimmed,
            truncated=len(trimmed) < len(rows),
            row_count=len(rows),
            artifact=artifact,
            checksum=checksum,
            filters=dict(filters or {}),
            sort=sort,
            error_code=error_code,
        )
        if not trimmed or len(result.model_dump_json().encode("utf-8")) <= MAX_TOOL_BYTES:
            return result
        trimmed.pop()


def _allowed_result_artifact(artifact: str, job_type: AnalysisType | None) -> bool:
    name = Path(artifact).name
    if name != artifact or "/" in artifact or "\\" in artifact:
        return False
    differential = name in {"differential_gene_counts.csv", "union_significant_genes.csv"} or name.endswith((".sig.csv", ".all.csv"))
    dem = name in {"differential_metabolite_counts.csv", "union_significant_metabolites.csv"} or name.endswith((".sig.csv", ".all.csv"))
    correlation = name.startswith(("T01_", "T02_", "T03_", "T04_", "T05_", "T06_")) and name.endswith(".csv")
    figure_json = any(name in allowed for allowed in _FIGURE_JSON_BY_ANALYSIS.values())
    if job_type is None:
        return differential or dem or correlation or figure_json
    if job_type is AnalysisType.DIFFERENTIAL:
        return differential or name in _FIGURE_JSON_BY_ANALYSIS[job_type]
    if job_type is AnalysisType.DEM:
        return dem or name in _FIGURE_JSON_BY_ANALYSIS[job_type]
    return correlation or name in _FIGURE_JSON_BY_ANALYSIS[job_type]


def _query_json_evidence(
    *,
    text: str,
    artifact: str,
    checksum: str | None,
    field_path: str | None,
    filters: Mapping[str, Any] | None,
    sort: str | None,
    limit: int | None,
    resolve_entity: str | None,
) -> ToolResult:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return _tool_result(ToolName.QUERY_RESULT_EVIDENCE, rows=[], ok=False, error_code="invalid_json")
    if not _valid_json_filters(filters):
        return _tool_result(ToolName.QUERY_RESULT_EVIDENCE, rows=[], ok=False, error_code="invalid_filter")
    try:
        rows = _json_evidence_rows(payload, field_path)
    except (KeyError, IndexError, TypeError, ValueError):
        return _tool_result(ToolName.QUERY_RESULT_EVIDENCE, rows=[], ok=False, error_code="invalid_field")

    effective_filters = dict(filters or {})
    rows = [
        row for row in rows
        if all(str(row.get(key, "")) == str(value) for key, value in effective_filters.items())
    ]
    if resolve_entity:
        rows = [row for row in rows if _json_row_matches_entity(row, resolve_entity)]
    if sort:
        sort_field, _, direction = sort.partition(" ")
        reverse = direction.strip().lower() == "desc"
        if not all(sort_field in row for row in rows):
            return _tool_result(ToolName.QUERY_RESULT_EVIDENCE, rows=[], ok=False, error_code="invalid_sort")
        rows.sort(key=lambda row: _sort_value(row.get(sort_field)), reverse=reverse)

    max_rows = min(MAX_TOOL_ROWS, max(1, int(limit or MAX_TOOL_ROWS)))
    selected = rows[:max_rows]
    result = _tool_result(
        ToolName.QUERY_RESULT_EVIDENCE,
        rows=selected,
        artifact=artifact,
        checksum=checksum or "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest(),
        filters=effective_filters,
        sort=sort,
    )
    result.row_count = len(rows)
    result.truncated = result.truncated or len(selected) < len(rows)
    return result


def _json_evidence_rows(payload: Any, field_path: str | None) -> list[dict[str, Any]]:
    parts = field_path.split(".") if field_path else []
    if len(parts) > 12 or any(not part or not _JSON_PATH_PART.fullmatch(part) for part in parts):
        raise ValueError("invalid JSON field path")
    selected = payload
    for part in parts:
        if isinstance(selected, Mapping):
            selected = selected[part]
        elif isinstance(selected, list) and part.isdigit():
            selected = selected[int(part)]
        else:
            raise TypeError("JSON field path does not resolve")

    if isinstance(selected, list):
        return [
            _json_row(item, row_id=index + 1)
            for index, item in enumerate(selected)
        ]
    return [_json_row(selected, row_id=1)]


def _json_row(value: Any, *, row_id: int) -> dict[str, Any]:
    row: dict[str, Any] = {"_row_id": row_id}
    if isinstance(value, Mapping):
        row.update({str(key): item for key, item in value.items() if _is_json_scalar(item)})
        if len(row) == 1:
            raise TypeError("selected JSON object has no scalar fields")
    elif _is_json_scalar(value):
        row["value"] = value
    else:
        raise TypeError("selected JSON value is not a scalar or object row")
    return row


def _is_json_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _valid_json_filters(filters: Mapping[str, Any] | None) -> bool:
    if filters is None:
        return True
    return (
        len(filters) <= 8
        and all(isinstance(key, str) and 0 < len(key) <= 100 for key in filters)
        and all(_is_json_scalar(value) for value in filters.values())
    )


def _json_row_matches_entity(row: Mapping[str, Any], entity: str) -> bool:
    lowered = {str(key).lower(): value for key, value in row.items()}
    return any(str(lowered.get(field, "")) == entity for field in _JSON_ENTITY_FIELDS)


def _inspect_input(field: str, item: AgentInputFile) -> dict[str, Any]:
    parsed = list(csv.reader(io.StringIO(item.content.decode("utf-8-sig", errors="replace"))))
    headers = parsed[0] if parsed else []
    data_rows = parsed[1:]
    column_count = len(headers)
    columns = headers if column_count <= 12 else headers[:10] + headers[-2:]
    row = {
        "field": field,
        "filename": item.filename,
        "columns": columns,
        "column_count": column_count,
        "row_count": len(data_rows),
        "size_bytes": len(item.content),
    }
    if field in {"counts", "metabs", "transcriptome", "metabolome"}:
        values = [cell.strip() for cells in data_rows for cell in cells[1:]]
        present = [cell for cell in values if cell]
        feature_ids = [
            cells[0].strip()
            for cells in data_rows
            if cells and cells[0].strip()
        ]
        feature_id_total = len(feature_ids)
        sample_positions = list(range(min(10, feature_id_total)))
        for position in (
            feature_id_total // 4,
            feature_id_total // 2,
            (3 * feature_id_total) // 4,
            (7 * feature_id_total) // 8,
            feature_id_total - 1,
        ):
            if 0 <= position < feature_id_total and position not in sample_positions:
                sample_positions.append(position)
        sample_positions.sort()
        feature_id_sample = [feature_ids[position][:48] for position in sample_positions]
        numeric: list[float] = []
        for cell in present:
            try:
                numeric.append(float(cell))
            except ValueError:
                continue
        row.update({
            "dtype": "numeric" if len(numeric) == len(present) else "mixed",
            "min": min(numeric) if numeric else None,
            "max": max(numeric) if numeric else None,
            "has_negative": any(value < 0 for value in numeric),
            "integer_ratio": (sum(value.is_integer() for value in numeric) / len(numeric)) if numeric else 0.0,
            "missing_rate": ((len(values) - len(present)) / len(values)) if values else 0.0,
            "feature_id_sample": feature_id_sample,
            "feature_id_total": feature_id_total,
        })
    else:
        records = _dict_rows(item.content)
        group_replicates: dict[str, dict[str, int]] = {}
        for header in headers:
            if header == "sample_id":
                continue
            counts: dict[str, int] = {}
            for record in records:
                value = record.get(header, "")
                counts[value] = counts.get(value, 0) + 1
            group_replicates[header] = counts
        row["group_replicates"] = group_replicates
        if field in {"metadata", "group"} and len(data_rows) <= 60 and column_count <= 10:
            row["raw_rows"] = [
                [str(cell).strip()[:60] for cell in cells[:10]]
                for cells in data_rows[:60]
            ]
    return row


def _bounded_preflight_file(item: Any) -> dict[str, Any]:
    return {
        "field": item.field,
        "filename": item.filename,
        "rows": item.rows,
        "columns": item.columns,
        "sample_count": len(item.sample_ids or item.sample_names),
        "feature_count": len(item.feature_ids),
        "duplicate_count": len(item.duplicate_ids),
        "duplicate_ids": list(item.duplicate_ids[:10]),
        "empty_column_count": len(item.empty_columns),
        "empty_columns": list(item.empty_columns[:10]),
        "required_columns": list(item.required_columns[:10]),
        "non_numeric_cells": item.non_numeric_cells,
        "row_length_issues": item.row_length_issues,
    }


def _alignment_diagnostic(analysis_type: AnalysisType, inputs: Mapping[str, AgentInputFile]) -> dict[str, Any] | None:
    """基于已解析的样本名生成有界对齐诊断；不含路径、DSN 或原始内容。"""
    matrix_fields = {
        AnalysisType.DIFFERENTIAL: "counts",
        AnalysisType.DEM: "metabs",
        AnalysisType.CORRELATION: "transcriptome",
    }
    matrix_field = matrix_fields[analysis_type]
    other_field = "metadata" if analysis_type in {AnalysisType.DIFFERENTIAL, AnalysisType.DEM} else "metabolome"
    matrix = inputs.get(matrix_field)
    other = inputs.get(other_field)
    if matrix is None or other is None:
        return None
    matrix_rows = list(csv.reader(io.StringIO(matrix.content.decode("utf-8-sig", errors="replace"))))
    matrix_ids = [str(item).strip() for item in (matrix_rows[0][1:] if matrix_rows else []) if str(item).strip()]
    if other_field == "metadata":
        other_ids = [str(row.get("sample_id") or "").strip() for row in _dict_rows(other.content)]
    else:
        other_rows = list(csv.reader(io.StringIO(other.content.decode("utf-8-sig", errors="replace"))))
        other_ids = [str(item).strip() for item in (other_rows[0][1:] if other_rows else []) if str(item).strip()]
    matrix_set, other_set = set(matrix_ids), set(other_ids)
    missing = sorted(matrix_set - other_set)
    extra = sorted(other_set - matrix_set)
    return {
        "matched": len(matrix_set & other_set),
        "missing_from_metadata": [item[:60] for item in missing[:10]],
        "extra_in_metadata": [item[:60] for item in extra[:10]],
        "pattern_hint": _alignment_pattern_hint(matrix_set, other_set) if missing or extra else None,
    }


def _alignment_pattern_hint(left: set[str], right: set[str]) -> str | None:
    if {item.lower() for item in left} == {item.lower() for item in right}:
        return "大小写差异"
    normalize = lambda value: re.sub(r"[-_]", "", value).lower()
    if {normalize(item) for item in left} == {normalize(item) for item in right}:
        return "分隔符差异（- 与 _）"
    if len(left) == len(right) and left and right:
        left_parts = [re.match(r"^(.*?)(\d+)$", item) for item in left]
        right_parts = [re.match(r"^(.*?)(\d+)$", item) for item in right]
        if all(left_parts) and all(right_parts):
            left_suffixes = {match.group(2) for match in left_parts if match}
            right_suffixes = {match.group(2) for match in right_parts if match}
            prefixes = {match.group(1).lower() for match in [*left_parts, *right_parts] if match}
            if left_suffixes == right_suffixes and len(prefixes) > 1:
                return "统一前缀差异"
    return None


def _sort_value(value: str | None) -> tuple[int, object]:
    if value is None:
        return (0, "")
    try:
        return (1, float(value))
    except (TypeError, ValueError):
        return (2, str(value))


def _verify_input_checksum(content: bytes, expected_checksum: str | None, field: str) -> None:
    if not expected_checksum:
        return
    expected = str(expected_checksum).removeprefix("sha256:")
    actual = hashlib.sha256(content).hexdigest()
    if expected != actual:
        raise ToolConfigurationError(f"input {field} checksum changed")
