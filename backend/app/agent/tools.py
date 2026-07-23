from __future__ import annotations

import csv
import hashlib
import io
import json
from pathlib import Path
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from fastapi import HTTPException, UploadFile

from ..analysis_specs import AnalysisSpecRegistry
from ..models import AnalysisType, JobOwnerType, JobRecord, JobStatus
from ..preflight import PreflightService, build_contrast_preview
from .schemas import ToolName, ToolResult
from .approvals import ApprovalGate
from .plans import PlanNotFound, PlanStore, compute_plan_hash
from .policy import PolicyGuard
from .schemas import ActiveProfile


MAX_TOOL_ROWS = 50
MAX_TOOL_BYTES = 32 * 1024


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


@dataclass
class AgentToolRuntime:
    """由 API 会话创建的工具运行时；身份和服务句柄不会进入模型上下文。"""

    user_id: str
    inputs: Mapping[str, AgentInputFile]
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
        job = job_store.get_for_user(source_job_id, user_id)
        inputs: dict[str, AgentInputFile] = {}
        for item in job.inputs:
            with files.open_artifact(job.id, item.path) as handle:
                content = handle.read(50 * 1024 * 1024 + 1)
            if len(content) > 50 * 1024 * 1024:
                raise ToolConfigurationError(f"input {item.field} exceeds 50 MB")
            inputs[item.field] = AgentInputFile(item.filename, bytes(content))
        return cls(
            user_id=user_id,
            inputs=inputs,
            input_source_job_id=source_job_id,
            plans=plans,
            job_store=job_store,
            files=files,
            executor=executor,
            approval_gate=approval_gate,
        )

    def __post_init__(self) -> None:
        if not self.user_id:
            raise ToolConfigurationError("session user_id is required")
        if self.analysis_specs is None:
            self.analysis_specs = AnalysisSpecRegistry()
        if self.preflight_service is None:
            self.preflight_service = PreflightService()

    def inspect_uploaded_inputs(self) -> ToolResult:
        rows = [_inspect_input(field, item) for field, item in sorted(self.inputs.items())]
        return _tool_result(ToolName.INSPECT_UPLOADED_INPUTS, rows=rows)

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
        requested_params = dict(params)
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
            "files": [item.model_dump(mode="json") for item in response.files],
            "errors": errors,
            "warnings": warnings,
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
        if not plan.contrasts:
            return _tool_result(ToolName.SUBMIT_APPROVED_PLAN, rows=[], ok=False, error_code="preflight_blocked")
        if compute_plan_hash(plan) != plan.plan_hash:
            return _tool_result(ToolName.SUBMIT_APPROVED_PLAN, rows=[], ok=False, error_code="plan_hash_mismatch")
        if self.input_source_job_id != plan.source_job_id:
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
        source = self.job_store.get_for_user(plan.source_job_id, self.user_id)
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
            inputs = [
                self.files.copy_input_artifact(source.id, job_id, item)
                for item in source.inputs
            ]
            job = JobRecord(
                id=job_id,
                project_id=source.project_id or job_id,
                project_name=source.project_name,
                analysis_type=plan.analysis_type,
                status=JobStatus.QUEUED,
                created_at=now,
                updated_at=now,
                owner_type=JobOwnerType.USER,
                owner_id=self.user_id,
                owner_label=source.owner_label,
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
            })
        return _tool_result(ToolName.GET_JOBS_STATUS, rows=rows)

    def query_result_evidence(
        self,
        job_id: str,
        artifact: str,
        filters: Mapping[str, Any] | None = None,
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
    if job_type is None:
        return differential or dem or correlation
    if job_type is AnalysisType.DIFFERENTIAL:
        return differential
    if job_type is AnalysisType.DEM:
        return dem
    return correlation


def _inspect_input(field: str, item: AgentInputFile) -> dict[str, Any]:
    parsed = list(csv.reader(io.StringIO(item.content.decode("utf-8-sig", errors="replace"))))
    headers = parsed[0] if parsed else []
    data_rows = parsed[1:]
    row = {
        "field": field,
        "filename": item.filename,
        "columns": headers,
        "row_count": len(data_rows),
        "size_bytes": len(item.content),
    }
    if field in {"counts", "metabs", "transcriptome", "metabolome"}:
        values = [cell.strip() for cells in data_rows for cell in cells[1:]]
        present = [cell for cell in values if cell]
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
    return row


def _sort_value(value: str | None) -> tuple[int, object]:
    if value is None:
        return (0, "")
    try:
        return (1, float(value))
    except (TypeError, ValueError):
        return (2, str(value))
