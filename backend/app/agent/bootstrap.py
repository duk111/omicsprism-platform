from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
from uuid import NAMESPACE_URL, uuid5

from fastapi import HTTPException

from ..job_execution import JobExecutor
from ..job_store import JobStorageService
from ..models import AnalysisType, JobOwnerType, JobRecord, JobStatus
from ..settings import AppSettings
from ..storage_service import CSV_MAX_BYTES, FileStorageService
from .approvals import ApprovalGate, PostgresApprovalGate
from .plans import PlanStore, PostgresPlanStore
from .product_store import AgentProductStore, PostgresAgentProductStore
from .graph import (
    AnalysisExecutionRequest,
    DatasetLoadRequest,
    DatasetLoader,
    JobLookupRequest,
    JobRef,
    JobSummary,
    ResultEvidenceRequest,
    build_agent_graph,
)
from .model import VllmGraphModel
from .nodes.result_qa import job_reader_from_runtime, result_querier_from_runtime
from .schemas import ToolResult
from .store import PostgresStateStore, StateStore
from .tools import AgentToolRuntime
from .validation import DatasetRef


@dataclass(frozen=True)
class AgentApiContext:
    """Application-scoped legacy stores and optional compiled v3 graph."""

    product_store: AgentProductStore
    state_store: StateStore
    plan_store: PlanStore
    approval_gate: ApprovalGate
    job_store: JobStorageService
    files: FileStorageService | None
    graph: object | None = None
    dataset_loader: DatasetLoader | None = None
    stream_poll_seconds: float = 1.0


def create_agent_api_context(
    settings: AppSettings,
    *,
    files: FileStorageService,
    job_store: JobStorageService,
    job_executor: JobExecutor | None = None,
) -> AgentApiContext | None:
    if settings.storage_backend != "postgres" or not settings.runtime_database_url:
        return None
    database_url = settings.runtime_database_url
    product_store = PostgresAgentProductStore(database_url)
    context = AgentApiContext(
        product_store=product_store,
        state_store=PostgresStateStore(database_url),
        plan_store=PostgresPlanStore(database_url),
        approval_gate=PostgresApprovalGate(database_url),
        job_store=job_store,
        files=files,
        stream_poll_seconds=max(0.1, settings.agent_poll_seconds),
    )
    # PHASE-5-DELETE: remove this branch and the legacy context dependencies.
    if not settings.use_v3_agent:
        return context
    if not settings.agent_model_url or not settings.agent_model_name:
        raise RuntimeError(
            "OMICS_PRISM_AGENT_MODEL_URL and OMICS_PRISM_AGENT_MODEL_NAME are required "
            "when OMICS_PRISM_USE_V3_AGENT=true"
        )
    if job_executor is None:
        raise RuntimeError("A Job executor is required when OMICS_PRISM_USE_V3_AGENT=true")

    model = VllmGraphModel(
        base_url=settings.agent_model_url,
        model=settings.agent_model_name,
        api_key=settings.agent_model_api_key,
        timeout_seconds=settings.agent_model_request_timeout_seconds,
    )

    def load_datasets(request: DatasetLoadRequest) -> list[DatasetRef]:
        refs: list[DatasetRef] = []
        for file_id in request.dataset_ids:
            item = product_store.get_input_file(file_id=file_id, user_id=request.user_id)
            with files.open_storage_key(item.storage_key) as handle:
                content = handle.read(CSV_MAX_BYTES + 1)
            if not content or len(content) > CSV_MAX_BYTES:
                raise ValueError(f"dataset {file_id} is empty or exceeds 50 MB")
            checksum = "sha256:" + sha256(content).hexdigest()
            if checksum.casefold() != item.checksum.casefold():
                raise ValueError(f"dataset {file_id} checksum changed")
            refs.append(DatasetRef(
                dataset_id=item.file_id,
                owner_id=item.user_id,
                role=item.field,
                filename=item.filename,
                checksum=item.checksum,
                content=bytes(content),
            ))
        return refs

    def submit_job(request: AnalysisExecutionRequest) -> JobRef:
        job_id = str(uuid5(
            NAMESPACE_URL,
            f"omicsprism:{request.user_id}:{request.idempotency_key}",
        ))
        try:
            existing = job_store.get_for_user(job_id, request.user_id)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            existing = None
        except (KeyError, LookupError):
            existing = None
        if existing is not None:
            return JobRef(job_id=existing.id, owner_id=request.user_id)

        input_records = [
            product_store.get_input_file(file_id=file_id, user_id=request.user_id)
            for file_id in request.dataset_ids
        ]
        inputs = [files.copy_staged_input(job_id, item) for item in input_records]
        analysis_type = {
            "DEG": AnalysisType.DIFFERENTIAL,
            "DEM": AnalysisType.DEM,
            "GMA": AnalysisType.CORRELATION,
        }[request.resolved_params.analysis_type]
        now = datetime.now(timezone.utc)
        job = JobRecord(
            id=job_id,
            project_id=job_id,
            project_name="Copilot analysis",
            analysis_type=analysis_type,
            status=JobStatus.QUEUED,
            created_at=now,
            updated_at=now,
            owner_type=JobOwnerType.USER,
            owner_id=request.user_id,
            inputs=inputs,
            params=request.resolved_params.legacy_params(),
            progress=0,
            progress_step="Queued",
        )
        job_store.save(job)
        job_executor.enqueue(job_id)
        return JobRef(job_id=job_id, owner_id=request.user_id)

    def read_job(request: JobLookupRequest) -> JobSummary:
        runtime = AgentToolRuntime(
            user_id=request.user_id,
            job_store=job_store,
            files=files,
        )
        return job_reader_from_runtime(runtime)(request)

    def query_result(request: ResultEvidenceRequest) -> ToolResult:
        runtime = AgentToolRuntime(
            user_id=request.user_id,
            job_store=job_store,
            files=files,
        )
        return result_querier_from_runtime(runtime)(request)

    graph = build_agent_graph(
        model,
        load_datasets,
        submit_job,
        read_job,
        query_result,
    )
    return replace(context, graph=graph, dataset_loader=load_datasets)
