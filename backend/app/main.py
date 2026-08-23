from __future__ import annotations

import csv
import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Annotated, Any
from uuid import uuid4

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from .agent.api import create_agent_router
from .agent.bootstrap import create_agent_api_context
from .bootstrap import create_context
from .errors import (
    ApiErrorDetail,
    ErrorCategory,
    analysis_failure_detail,
    category_for_status,
    default_user_message,
    input_error,
    suggestions_for_category,
)
from .models import (
    AnalysisType,
    FigureDataResponse,
    ImageInfo,
    JobFilesResponse,
    JobListResponse,
    JobLogResponse,
    JobOwnerType,
    JobParams,
    JobProgressResponse,
    JobRecord,
    JobResponse,
    JobStatus,
    PreflightResponse,
)
from .observability import configure_logging, current_request_id, log_context
from .preflight import PreflightService
from .settings import load_settings


SETTINGS = load_settings()
configure_logging(SETTINGS.log_level)
CONTEXT = create_context(
    SETTINGS,
    ensure_figure_specs=lambda job_id: _ensure_figure_specs(job_id),
    remaining_seconds=lambda job, progress: _remaining_seconds(job, progress),
)
FILES = CONTEXT.files
JOB_STORE = CONTEXT.job_store
PREFLIGHT = PreflightService()
JOB_RUNNER = CONTEXT.job_runner
JOB_EXECUTOR = CONTEXT.job_executor
AGENT_API_CONTEXT = create_agent_api_context(
    SETTINGS,
    files=FILES,
    job_store=JOB_STORE,
    job_executor=JOB_EXECUTOR,
)
LOG = logging.getLogger("omicsprism.platform.api")

SESSION_COOKIE = "omicsprism_session"

app = FastAPI(title="OmicsPrism Platform API", version="0.5.0")

_cors_origins = os.getenv(
    "OMICS_PRISM_CORS_ORIGINS",
    "http://localhost:5173,http://127.0.0.1:5173",
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_context_middleware(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or str(uuid4())
    started = asyncio.get_running_loop().time()
    with log_context(request_id=request_id):
        request.state.request_id = request_id
        response = None
        try:
            response = await call_next(request)
            return response
        finally:
            status_code = getattr(response, "status_code", 500)
            duration_ms = round((asyncio.get_running_loop().time() - started) * 1000, 2)
            LOG.info(
                "request completed",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": status_code,
                    "duration_ms": duration_ms,
                },
            )
            if response is not None:
                response.headers["X-Request-ID"] = request_id


def get_session_id(request: Request, response: Response) -> str:
    session_id = request.cookies.get(SESSION_COOKIE)
    if not session_id:
        session_id = str(uuid4())
        response.set_cookie(
            SESSION_COOKIE,
            session_id,
            httponly=True,
            secure=False,
            samesite="lax",
            max_age=60 * 60 * 24 * 30,
            path="/",
        )
    return session_id


app.include_router(create_agent_router(
    context=AGENT_API_CONTEXT,
    session_dependency=get_session_id,
))


def require_job_access(job_id: str, session_id: str) -> JobRecord:
    job = JOB_STORE.get_for_user(job_id, session_id)
    if job.deleted_at is not None:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    detail = _normalize_error_detail(exc.status_code, exc.detail)
    request_id = getattr(request.state, "request_id", None)
    if request_id:
        detail.context = {**detail.context, "request_id": request_id}
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": detail.model_dump(mode="json")},
        headers={"X-Request-ID": request_id or ""},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    detail = ApiErrorDetail(
        category=ErrorCategory.INPUT,
        code="request_validation_failed",
        message="Request validation failed",
        user_message="Some required fields are missing or have an invalid format.",
        suggestions=["Check that all required files and parameters are filled in."],
        technical_detail=str(exc),
        context={"errors": exc.errors()},
    )
    request_id = getattr(request.state, "request_id", None)
    if request_id:
        detail.context = {**detail.context, "request_id": request_id}
    return JSONResponse(
        status_code=422,
        content={"detail": detail.model_dump(mode="json")},
        headers={"X-Request-ID": request_id or ""},
    )


def _normalize_error_detail(status_code: int, detail: Any) -> ApiErrorDetail:
    if isinstance(detail, dict) and {"category", "code", "user_message"}.issubset(detail):
        return ApiErrorDetail.model_validate(detail)
    if isinstance(detail, dict) and "errors" in detail and "warnings" in detail:
        errors = detail.get("errors") or []
        message = errors[0].get("message") if errors and isinstance(errors[0], dict) else "Preflight validation failed"
        return ApiErrorDetail(
            category=ErrorCategory.INPUT,
            code="preflight_failed",
            message=str(message),
            user_message="The uploaded CSV files did not pass preflight validation.",
            suggestions=["Fix the CSV schema issues and try again."],
            technical_detail=json.dumps(detail, ensure_ascii=False),
            context={"preflight": detail},
        )
    category = category_for_status(status_code)
    message = detail if isinstance(detail, str) else default_user_message(category)
    return ApiErrorDetail(
        category=category,
        code="system_error",
        message=str(message),
        user_message=str(message),
        suggestions=suggestions_for_category(category),
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/jobs/preflight", response_model=PreflightResponse)
async def preflight_job(
    analysis_type: Annotated[str, Form()],
    counts: Annotated[UploadFile | None, File()] = None,
    metadata: Annotated[UploadFile | None, File()] = None,
    metabs: Annotated[UploadFile | None, File()] = None,
    transcriptome: Annotated[UploadFile | None, File()] = None,
    metabolome: Annotated[UploadFile | None, File()] = None,
    group: Annotated[UploadFile | None, File()] = None,
    compare_field: Annotated[str | None, Form()] = None,
    tested_levels: Annotated[str | None, Form()] = None,
    reference_level: Annotated[str | None, Form()] = None,
    padj_cutoff: Annotated[float | None, Form()] = None,
    log2fc_cutoff: Annotated[float | None, Form()] = None,
    min_total_count: Annotated[int | None, Form()] = None,
    min_replicates: Annotated[int | None, Form()] = None,
    same_fields: Annotated[str | None, Form()] = None,
    normalize: Annotated[bool | None, Form()] = None,
    filter_low_expression: Annotated[bool | None, Form()] = None,
    method: Annotated[str | None, Form()] = None,
    fdr_cutoff: Annotated[float | None, Form()] = None,
    enable_modules: Annotated[bool | None, Form()] = None,
    vip_cutoff: Annotated[float | None, Form()] = None,
    pseudocount: Annotated[float | None, Form()] = None,
    max_missing_fraction: Annotated[float | None, Form()] = None,
    impute_method: Annotated[str | None, Form()] = None,
    log_transform: Annotated[bool | None, Form()] = None,
    trans_log2: Annotated[bool | None, Form()] = None,
    metab_log2: Annotated[bool | None, Form()] = None,
    n_orthogonal_components: Annotated[int | None, Form()] = None,
) -> PreflightResponse:
    atype = _parse_analysis_type(analysis_type)
    params = _build_job_params(
        atype=atype,
        counts=counts, metadata=metadata, metabs=metabs,
        transcriptome=transcriptome, metabolome=metabolome, group=group,
        compare_field=compare_field, tested_levels=tested_levels,
        reference_level=reference_level, padj_cutoff=padj_cutoff,
        log2fc_cutoff=log2fc_cutoff, min_total_count=min_total_count,
        min_replicates=min_replicates, same_fields=same_fields,
        normalize=normalize, filter_low_expression=filter_low_expression,
        method=method, fdr_cutoff=fdr_cutoff, enable_modules=enable_modules,
        vip_cutoff=vip_cutoff, pseudocount=pseudocount,
        max_missing_fraction=max_missing_fraction,
        impute_method=impute_method, log_transform=log_transform,
        trans_log2=trans_log2, metab_log2=metab_log2,
        n_orthogonal_components=n_orthogonal_components,
    )
    file_map = {name: f for name, f in {
        "counts": counts, "metadata": metadata, "metabs": metabs,
        "transcriptome": transcriptome, "metabolome": metabolome, "group": group,
    }.items() if f is not None}
    return PREFLIGHT.preflight(atype, params=params, files=file_map)


@app.post("/api/jobs", response_model=JobResponse, status_code=201)
async def create_job(
    response: Response,
    analysis_type: Annotated[str, Form()],
    counts: Annotated[UploadFile | None, File()] = None,
    metadata: Annotated[UploadFile | None, File()] = None,
    metabs: Annotated[UploadFile | None, File()] = None,
    transcriptome: Annotated[UploadFile | None, File()] = None,
    metabolome: Annotated[UploadFile | None, File()] = None,
    group: Annotated[UploadFile | None, File()] = None,
    compare_field: Annotated[str | None, Form()] = None,
    tested_levels: Annotated[str | None, Form()] = None,
    reference_level: Annotated[str | None, Form()] = None,
    padj_cutoff: Annotated[float | None, Form()] = None,
    log2fc_cutoff: Annotated[float | None, Form()] = None,
    min_total_count: Annotated[int | None, Form()] = None,
    min_replicates: Annotated[int | None, Form()] = None,
    same_fields: Annotated[str | None, Form()] = None,
    normalize: Annotated[bool | None, Form()] = None,
    filter_low_expression: Annotated[bool | None, Form()] = None,
    method: Annotated[str | None, Form()] = None,
    fdr_cutoff: Annotated[float | None, Form()] = None,
    enable_modules: Annotated[bool | None, Form()] = None,
    vip_cutoff: Annotated[float | None, Form()] = None,
    pseudocount: Annotated[float | None, Form()] = None,
    max_missing_fraction: Annotated[float | None, Form()] = None,
    impute_method: Annotated[str | None, Form()] = None,
    log_transform: Annotated[bool | None, Form()] = None,
    trans_log2: Annotated[bool | None, Form()] = None,
    metab_log2: Annotated[bool | None, Form()] = None,
    n_orthogonal_components: Annotated[int | None, Form()] = None,
    request: Request = None,
) -> JobResponse:
    session_id = get_session_id(request, response)
    atype = _parse_analysis_type(analysis_type)
    params = _build_job_params(
        atype=atype,
        counts=counts, metadata=metadata, metabs=metabs,
        transcriptome=transcriptome, metabolome=metabolome, group=group,
        compare_field=compare_field, tested_levels=tested_levels,
        reference_level=reference_level, padj_cutoff=padj_cutoff,
        log2fc_cutoff=log2fc_cutoff, min_total_count=min_total_count,
        min_replicates=min_replicates, same_fields=same_fields,
        normalize=normalize, filter_low_expression=filter_low_expression,
        method=method, fdr_cutoff=fdr_cutoff, enable_modules=enable_modules,
        vip_cutoff=vip_cutoff, pseudocount=pseudocount,
        max_missing_fraction=max_missing_fraction,
        impute_method=impute_method, log_transform=log_transform,
        trans_log2=trans_log2, metab_log2=metab_log2,
        n_orthogonal_components=n_orthogonal_components,
    )
    files = {
        "counts": counts, "metadata": metadata, "metabs": metabs,
        "transcriptome": transcriptome, "metabolome": metabolome, "group": group,
    }
    preflight = PREFLIGHT.preflight(atype, params=params, files={k: v for k, v in files.items() if v is not None})
    if not preflight.can_submit:
        raise HTTPException(status_code=422, detail=preflight.model_dump())

    job_id = str(uuid4())
    FILES.prepare_run_dirs(job_id)

    inputs = []
    if atype == AnalysisType.DIFFERENTIAL:
        inputs.append(await FILES.save_upload(job_id, "counts", files["counts"], "counts.csv"))
        inputs.append(await FILES.save_upload(job_id, "metadata", files["metadata"], "metadata.csv"))
    elif atype == AnalysisType.DEM:
        inputs.append(await FILES.save_upload(job_id, "metabs", files["metabs"], "metabs.csv"))
        inputs.append(await FILES.save_upload(job_id, "metadata", files["metadata"], "metadata.csv"))
    else:
        inputs.append(await FILES.save_upload(job_id, "transcriptome", files["transcriptome"], "transcriptome.csv"))
        inputs.append(await FILES.save_upload(job_id, "metabolome", files["metabolome"], "metabolome.csv"))
        inputs.append(await FILES.save_upload(job_id, "group", files["group"], "group.csv"))

    now = datetime.now(timezone.utc)
    input_paths = {f.field: FILES.resolve_run_file(job_id, f.path) for f in inputs}
    timing = _estimate_job_timing(atype, params, input_paths)

    job = JobRecord(
        id=job_id,
        project_id=job_id,
        project_name=atype.value,
        analysis_type=atype,
        status=JobStatus.QUEUED,
        is_demo=False,
        created_at=now,
        updated_at=now,
        owner_type=JobOwnerType.PROJECT,
        owner_id=session_id,
        owner_label="session",
        inputs=inputs,
        params=params,
        attempt=0,
        max_retries=0,
        progress=0,
        progress_step="Queued",
        estimated_total_seconds=timing["estimated_total_seconds"],
        estimated_remaining_seconds=timing["estimated_remaining_seconds"],
        estimated_range_min_seconds=timing["estimated_range_min_seconds"],
        estimated_range_max_seconds=timing["estimated_range_max_seconds"],
        estimated_range_label=timing["estimated_range_label"],
    )
    JOB_STORE.save(job)

    if JOB_EXECUTOR is None:
        raise HTTPException(status_code=503, detail="Job executor is not configured")
    JOB_EXECUTOR.enqueue(job_id)
    LOG.info("job submitted", extra={"job_id": job_id, "analysis_type": atype.value})
    return _to_job_response(job)


@app.get("/api/jobs", response_model=JobListResponse)
def list_jobs(request: Request, response: Response) -> JobListResponse:
    session_id = get_session_id(request, response)
    jobs = JOB_STORE.list_for_user(session_id)
    return JobListResponse(jobs=[_to_job_response(job) for job in jobs])


@app.get("/api/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: str, request: Request, response: Response) -> JobResponse:
    session_id = get_session_id(request, response)
    return _to_job_response(require_job_access(job_id, session_id))


@app.get("/api/jobs/{job_id}/progress", response_model=JobProgressResponse)
def get_job_progress(job_id: str, request: Request, response: Response) -> JobProgressResponse:
    session_id = get_session_id(request, response)
    return _to_job_progress_response(require_job_access(job_id, session_id))


@app.get("/api/jobs/{job_id}/progress/events")
async def stream_job_progress(job_id: str, request: Request) -> StreamingResponse:
    session_id = request.cookies.get(SESSION_COOKIE) or ""

    async def event_stream():
        previous_payload = ""
        while True:
            if await request.is_disconnected():
                break
            try:
                job = require_job_access(job_id, session_id)
            except HTTPException:
                break
            progress = _to_job_progress_response(job)
            payload = progress.model_dump_json()
            if payload != previous_payload:
                previous_payload = payload
                yield f"event: progress\ndata: {payload}\n\n"
            if progress.status in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}:
                yield f"event: complete\ndata: {payload}\n\n"
                break
            await asyncio.sleep(2)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@app.get("/api/jobs/{job_id}/logs", response_model=JobLogResponse)
def get_job_logs(job_id: str, request: Request, response: Response) -> JobLogResponse:
    session_id = get_session_id(request, response)
    require_job_access(job_id, session_id)
    log_name, content = FILES.read_log(job_id)
    return JobLogResponse(job_id=job_id, log_name=log_name, content=content)


@app.get("/api/jobs/{job_id}/files", response_model=JobFilesResponse)
def get_job_files(job_id: str, request: Request, response: Response) -> JobFilesResponse:
    session_id = get_session_id(request, response)
    job = require_job_access(job_id, session_id)
    return JobFilesResponse(job_id=job.id, inputs=job.inputs, result_files=job.result_files, report_links=job.report_links)


@app.get("/api/jobs/{job_id}/images")
def list_job_images(job_id: str, request: Request, response: Response) -> list[ImageInfo]:
    session_id = get_session_id(request, response)
    require_job_access(job_id, session_id)
    return FILES.list_images(job_id)


@app.get("/api/jobs/{job_id}/figure-data/{figure_id}", response_model=FigureDataResponse)
def get_figure_data(job_id: str, figure_id: str, request: Request, response: Response) -> FigureDataResponse:
    session_id = get_session_id(request, response)
    require_job_access(job_id, session_id)
    relative_path = f"outputs/figure_data/{figure_id}.json"
    if not FILES.has_artifact(job_id, relative_path):
        fallback_path = _legacy_figure_data_path(job_id, figure_id)
        if fallback_path is None:
            raise HTTPException(status_code=404, detail=f"Figure data not found: {figure_id}")
        relative_path = fallback_path
    data = FILES.read_json_artifact(job_id, relative_path)
    return FigureDataResponse(**data)


def _legacy_figure_data_path(job_id: str, figure_id: str) -> str | None:
    fallback_names: dict[str, tuple[str, ...]] = {
        "pca": ("pca-scatter", "pca_scatter", "pca-pairs", "pca_pairs"),
        "bubble-heatmap": ("bubble_heatmap",),
        "module-heatmap": ("module_heatmap",),
        "line-panels": ("line_panels",),
        "scatter-panels": ("scatter_panels",),
        "violin-box": ("violin_box",),
        "bar-charts": ("bar_charts",),
    }
    for name in fallback_names.get(figure_id, ()):
        relative_path = f"outputs/figure_data/{name}.json"
        if FILES.has_artifact(job_id, relative_path):
            return relative_path
    return None


@app.get("/api/jobs/{job_id}/download/{relative_path:path}")
def download_job_file(job_id: str, relative_path: str, request: Request, response: Response) -> FileResponse:
    session_id = get_session_id(request, response)
    require_job_access(job_id, session_id)
    path = FILES.resolve_run_file(job_id, relative_path)
    if path.suffix.lower() == ".html":
        return FileResponse(
            path,
            media_type="text/html",
            filename=FILES.get_artifact_download_name(job_id, relative_path),
            content_disposition_type="inline",
        )
    return FileResponse(path, filename=FILES.get_artifact_download_name(job_id, relative_path))


@app.get("/api/jobs/{job_id}/reports/summary")
def open_summary_report(job_id: str, request: Request, response: Response) -> FileResponse:
    session_id = get_session_id(request, response)
    require_job_access(job_id, session_id)
    path = FILES.resolve_run_file(job_id, "outputs/OmicsPrism_Report.html")
    return FileResponse(path, media_type="text/html", filename=path.name)


@app.get("/api/jobs/{job_id}/reports/interactive")
def open_interactive_report(job_id: str, request: Request, response: Response) -> FileResponse:
    session_id = get_session_id(request, response)
    require_job_access(job_id, session_id)
    path = FILES.resolve_run_file(job_id, "outputs/OmicsPrism_Interactive_Report.html")
    return FileResponse(path, media_type="text/html", filename=path.name)


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str, request: Request, response: Response) -> dict[str, str]:
    session_id = get_session_id(request, response)
    job = require_job_access(job_id, session_id)
    if job.status not in {JobStatus.QUEUED, JobStatus.RUNNING}:
        raise HTTPException(status_code=409, detail="Only queued or running jobs can be cancelled")
    previous_status = job.status
    message = (
        "Queued job cancelled before worker execution."
        if job.status == JobStatus.QUEUED
        else "Cancellation requested."
    )
    JOB_STORE.update(
        job,
        status=JobStatus.CANCELLED,
        progress=job.progress,
        progress_step="Cancellation requested" if previous_status == JobStatus.RUNNING else "Cancelled",
        completed_at=datetime.now(timezone.utc),
        estimated_remaining_seconds=0,
    )
    return {"status": "ok", "message": message}


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str, request: Request, response: Response) -> dict[str, str]:
    session_id = get_session_id(request, response)
    job = require_job_access(job_id, session_id)
    if job.status in {JobStatus.QUEUED, JobStatus.RUNNING}:
        was_running = job.status == JobStatus.RUNNING
        JOB_STORE.update(
            job, status=JobStatus.CANCELLED, completed_at=datetime.now(timezone.utc),
            estimated_remaining_seconds=0, error=None,
        )
        if was_running:
            for _ in range(10):
                time.sleep(0.3)
                current = JOB_STORE.get_for_user(job_id, session_id)
                if current.progress_step and "cancelled" in current.progress_step.lower():
                    break
        job = JOB_STORE.get_for_user(job_id, session_id)
    FILES.cleanup_job_storage(job)
    deleted_at = datetime.now(timezone.utc)
    job.deleted_at = deleted_at
    job.updated_at = deleted_at
    JOB_STORE.save(job)
    return {"status": "ok"}


def _build_job_params(
    *,
    atype: AnalysisType,
    counts: UploadFile | None,
    metadata: UploadFile | None,
    metabs: UploadFile | None,
    transcriptome: UploadFile | None,
    metabolome: UploadFile | None,
    group: UploadFile | None,
    compare_field: str | None,
    tested_levels: str | None,
    reference_level: str | None,
    padj_cutoff: float | None,
    log2fc_cutoff: float | None,
    min_total_count: int | None,
    min_replicates: int | None,
    same_fields: str | None,
    normalize: bool | None,
    filter_low_expression: bool | None,
    method: str | None,
    fdr_cutoff: float | None,
    enable_modules: bool | None,
    vip_cutoff: float | None,
    pseudocount: float | None,
    max_missing_fraction: float | None,
    impute_method: str | None,
    log_transform: bool | None,
    trans_log2: bool | None,
    metab_log2: bool | None,
    n_orthogonal_components: int | None,
) -> JobParams:
    if atype == AnalysisType.DIFFERENTIAL:
        if counts is None:
            raise HTTPException(status_code=400, detail="counts is required for differential analysis")
        if metadata is None:
            raise HTTPException(status_code=400, detail="metadata is required for differential analysis")
        if not compare_field:
            raise HTTPException(status_code=400, detail="compare_field is required for differential analysis")
        if not tested_levels:
            raise HTTPException(status_code=400, detail="tested_levels is required for differential analysis")
        if not reference_level:
            raise HTTPException(status_code=400, detail="reference_level is required for differential analysis")
        return {
            "compare_field": compare_field,
            "tested_levels": tested_levels,
            "reference_level": reference_level,
            "padj_cutoff": padj_cutoff if padj_cutoff is not None else 0.05,
            "log2fc_cutoff": log2fc_cutoff if log2fc_cutoff is not None else 1.0,
            "min_total_count": min_total_count if min_total_count is not None else 10,
            "min_replicates": min_replicates if min_replicates is not None else 2,
            "same_fields": same_fields or "",
            "normalize": bool(normalize) if normalize is not None else True,
            "filter_low_expression": bool(filter_low_expression) if filter_low_expression is not None else True,
        }

    if atype == AnalysisType.DEM:
        if metabs is None:
            raise HTTPException(status_code=400, detail="metabs is required for DEM analysis")
        if metadata is None:
            raise HTTPException(status_code=400, detail="metadata is required for DEM analysis")
        if not compare_field:
            raise HTTPException(status_code=400, detail="compare_field is required for DEM analysis")
        if not tested_levels:
            raise HTTPException(status_code=400, detail="tested_levels is required for DEM analysis")
        if not reference_level:
            raise HTTPException(status_code=400, detail="reference_level is required for DEM analysis")
        normalized_impute = (impute_method or "half-min").strip().lower()
        if normalized_impute not in {"half-min", "median"}:
            raise HTTPException(status_code=400, detail="impute_method must be half-min or median")
        return {
            "compare_field": compare_field,
            "tested_levels": tested_levels,
            "reference_level": reference_level,
            "same_fields": same_fields or "",
            "padj_cutoff": padj_cutoff if padj_cutoff is not None else 0.05,
            "log2fc_cutoff": log2fc_cutoff if log2fc_cutoff is not None else 1.0,
            "vip_cutoff": vip_cutoff if vip_cutoff is not None else 1.0,
            "pseudocount": pseudocount if pseudocount is not None else 1e-9,
            "max_missing_fraction": max_missing_fraction if max_missing_fraction is not None else 0.5,
            "impute_method": normalized_impute,
            "normalize": bool(normalize) if normalize is not None else True,
            "log_transform": bool(log_transform) if log_transform is not None else True,
            "min_replicates": min_replicates if min_replicates is not None else 2,
            "n_orthogonal_components": n_orthogonal_components if n_orthogonal_components is not None else 1,
        }

    if transcriptome is None:
        raise HTTPException(status_code=400, detail="transcriptome is required for correlation analysis")
    if metabolome is None:
        raise HTTPException(status_code=400, detail="metabolome is required for correlation analysis")
    if group is None:
        raise HTTPException(status_code=400, detail="group is required for correlation analysis")

    normalized_method = (method or "Spearman").strip().lower()
    if normalized_method not in {"pearson", "spearman"}:
        raise HTTPException(status_code=400, detail="method must be Pearson or Spearman")
    return {
        "method": normalized_method,
        "fdr_cutoff": fdr_cutoff if fdr_cutoff is not None else 0.05,
        "enable_modules": True if enable_modules is None else bool(enable_modules),
        "trans_log2": bool(trans_log2) if trans_log2 is not None else True,
        "metab_log2": bool(metab_log2) if metab_log2 is not None else True,
    }


def _parse_analysis_type(analysis_type: str) -> AnalysisType:
    try:
        return AnalysisType(analysis_type)
    except ValueError:
        raise input_error(
            "invalid_analysis_type",
            f"Unknown analysis type: {analysis_type}",
            suggestions=["Choose Differential, DEM, or Correlation analysis."],
        ) from None


def _ensure_figure_specs(job_id: str) -> dict[str, Any]:
    manifest_figures: list[dict[str, Any]] = []
    for stem in FILES.discover_static_image_stems(job_id):
        source_paths = FILES.source_static_paths(job_id, stem)
        if not any(source_paths.values()):
            continue
        figure_id = _safe_figure_id(stem)
        spec = _build_figure_spec(job_id, figure_id, stem, source_paths)
        FILES.write_figure_spec(job_id, figure_id, spec)
        manifest_figures.append(
            {
                "figureId": figure_id,
                "title": spec["title"],
                "chartType": spec["chartType"],
                "interactiveMode": spec["interactiveMode"],
                "specPath": f"outputs/figures/{figure_id}.json",
                "thumbnailUrl": source_paths.get("png") or source_paths.get("svg") or source_paths.get("jpg") or "",
            }
        )

    manifest = {
        "jobId": job_id,
        "version": "figure-spec/v1",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "figures": manifest_figures,
    }
    FILES.write_figure_manifest(job_id, manifest)
    return manifest


def _build_figure_spec(
    job_id: str,
    figure_id: str,
    stem: str,
    source_paths: dict[str, str | None],
) -> dict[str, Any]:
    chart_type = _infer_chart_type(stem)
    partial = source_paths.get("pdf") is None and source_paths.get("svg") is None
    palette = _default_palette_for_chart(chart_type)
    controls = _default_controls_for_chart(chart_type, palette)
    allowed_controls = _allowed_controls_for_chart(chart_type, partial)
    encoding = _default_encoding_for_chart(chart_type)
    labels = _axis_labels_from_encoding(encoding)

    return {
        "schemaVersion": "figure-spec/v1",
        "figureId": figure_id,
        "title": _pretty_figure_title(stem),
        "chartType": chart_type,
        "interactiveMode": "partial" if partial else "full",
        "sourceStaticImagePaths": {
            "png": source_paths.get("png"),
            "svg": source_paths.get("svg"),
            "pdf": source_paths.get("pdf"),
        },
        "dataSourceTablePath": _infer_data_source_table(stem),
        "encoding": encoding,
        "xEncoding": encoding.get("x") if isinstance(encoding, dict) else None,
        "yEncoding": encoding.get("y") if isinstance(encoding, dict) else None,
        "colorEncoding": encoding.get("color") if isinstance(encoding, dict) else None,
        "sizeEncoding": encoding.get("size") if isinstance(encoding, dict) else None,
        "labels": labels,
        "axisRange": {},
        "legendOrder": [],
        "palette": palette,
        "thresholds": _default_thresholds_for_chart(chart_type),
        "sorting": _default_sorting_for_chart(chart_type),
        "facetLayout": _default_facet_layout_for_chart(chart_type),
        "defaultControls": controls,
        "controls": controls,
        "allowedControls": allowed_controls,
        "statistics": {},
        "annotations": [],
        "provenance": {
            "jobId": job_id,
            "sourceStem": stem,
            "sourcePaths": source_paths,
        },
    }


def _infer_chart_type(stem: str) -> str:
    text = stem.lower()
    if "volcano" in text:
        return "volcano"
    if "pca" in text or "oplsda" in text:
        return "pca"
    if "upset" in text or "evidence" in text:
        return "upset"
    if "cnet" in text:
        return "network"
    if "circos" in text:
        return "circos"
    if "network" in text:
        return "network"
    if "dendrogram" in text or "clustering" in text:
        return "dendrogram"
    if "heatmap" in text:
        return "heatmap"
    if "vip" in text or text.endswith(".bar") or ("count" in text and ("dem" in text or "metabolite" in text)):
        return "bar"
    if "regression" in text or "scatter" in text or "association" in text or "pairs" in text:
        return "scatter"
    if "score" in text and "zscore" not in text:
        return "pca"
    if "sankey" in text:
        return "static"
    return "static"


def _default_palette_for_chart(chart_type: str) -> dict[str, Any]:
    categorical = ["#1f77b4", "#d62728", "#2ca02c", "#9467bd", "#ff7f0e", "#17becf"]
    continuous = ["#2166ac", "#f7f7f7", "#b2182b"]
    return {
        "categorical": categorical,
        "continuous": continuous,
        "single": {
            "point": "#1f77b4",
            "line": "#1f2937",
            "threshold": "#dc2626",
            "background": "#ffffff",
            "edgePositive": "#dc2626",
            "edgeNegative": "#2563eb",
        },
        "active": "categorical" if chart_type in {"pca", "scatter", "network", "circos"} else "continuous",
    }


def _default_controls_for_chart(chart_type: str, palette: dict[str, Any]) -> dict[str, Any]:
    controls: dict[str, Any] = {
        "paletteMode": palette["active"],
        "categoricalPalette": palette["categorical"],
        "continuousPalette": palette["continuous"],
        "pointColor": palette["single"]["point"],
        "lineColor": palette["single"]["line"],
        "thresholdColor": palette["single"]["threshold"],
        "backgroundColor": palette["single"]["background"],
        "opacity": 1,
        "zoom": 1,
    }
    if chart_type == "pca":
        controls.update({"pointSize": 42, "showLabels": False, "showLegend": True})
    elif chart_type == "scatter":
        controls.update({"pointSize": 42, "showRegression": True, "showLabels": False})
    elif chart_type == "heatmap":
        controls.update({"cellBorderColor": "#ffffff", "showValues": False, "colorScaleMin": 0, "colorScaleMax": 1})
    elif chart_type == "volcano":
        controls.update({"pointSize": 18, "showThresholds": True})
    elif chart_type == "bar":
        controls.update({"sortDirection": "desc", "orientation": "vertical"})
    elif chart_type in {"network", "circos"}:
        controls.update({"nodeColor": "#2563eb", "edgePositiveColor": "#dc2626", "edgeNegativeColor": "#2563eb", "minEdgeWeight": 0})
    elif chart_type == "upset":
        controls.update({"sortBy": "size", "minIntersectionSize": 1})
    return controls


def _allowed_controls_for_chart(chart_type: str, partial: bool) -> list[dict[str, Any]]:
    common = [
        {"id": "paletteMode", "type": "select", "label": "Palette mode", "options": ["categorical", "continuous", "single"]},
        {"id": "categoricalPalette", "type": "palette", "label": "Categorical palette"},
        {"id": "continuousPalette", "type": "palette", "label": "Continuous palette"},
        {"id": "pointColor", "type": "color", "label": "Point color"},
        {"id": "lineColor", "type": "color", "label": "Line color"},
        {"id": "thresholdColor", "type": "color", "label": "Threshold color"},
        {"id": "backgroundColor", "type": "color", "label": "Background color"},
        {"id": "zoom", "type": "range", "label": "Zoom", "min": 0.5, "max": 3, "step": 0.1},
    ]
    if partial:
        return common
    if chart_type in {"pca", "scatter", "volcano"}:
        common.extend([
            {"id": "pointSize", "type": "range", "label": "Point size", "min": 4, "max": 120, "step": 1},
            {"id": "showLabels", "type": "toggle", "label": "Show labels"},
        ])
    if chart_type == "volcano":
        common.extend([
            {"id": "log2fcThreshold", "type": "range", "label": "|log2FC| threshold", "min": 0, "max": 5, "step": 0.1},
            {"id": "padjThreshold", "type": "range", "label": "padj threshold", "min": 0.001, "max": 0.1, "step": 0.001},
        ])
    if chart_type == "scatter":
        common.append({"id": "showRegression", "type": "toggle", "label": "Regression line"})
    if chart_type == "pca":
        common.append({"id": "showLegend", "type": "toggle", "label": "Show legend"})
    if chart_type == "heatmap":
        common.extend([
            {"id": "showValues", "type": "toggle", "label": "Show cell values"},
            {"id": "colorScaleMin", "type": "number", "label": "Color scale min"},
            {"id": "colorScaleMax", "type": "number", "label": "Color scale max"},
        ])
    if chart_type == "bar":
        common.extend([
            {"id": "sortDirection", "type": "select", "label": "Sort", "options": ["desc", "asc", "none"]},
            {"id": "orientation", "type": "select", "label": "Orientation", "options": ["vertical", "horizontal"]},
        ])
    if chart_type in {"network", "circos"}:
        common.extend([
            {"id": "minEdgeWeight", "type": "range", "label": "Min edge weight", "min": 0, "max": 1, "step": 0.01},
        ])
    if chart_type == "upset":
        common.extend([
            {"id": "sortBy", "type": "select", "label": "Sort by", "options": ["size", "degree"]},
        ])
    return common


def _default_encoding_for_chart(chart_type: str) -> dict[str, Any]:
    if chart_type == "pca":
        return {
            "x": {"field": "PC1", "type": "quantitative"},
            "y": {"field": "PC2", "type": "quantitative"},
            "color": {"field": "group", "type": "nominal"},
            "size": {"value": "pointSize"},
        }
    if chart_type == "volcano":
        return {
            "x": {"field": "log2FC", "type": "quantitative"},
            "y": {"field": "-log10(adjusted p-value)", "type": "quantitative"},
            "color": {"field": "significance", "type": "nominal"},
            "size": {"value": "pointSize"},
        }
    if chart_type == "scatter":
        return {
            "x": {"field": "gene/module value", "type": "quantitative"},
            "y": {"field": "metabolite value", "type": "quantitative"},
            "color": {"field": "group", "type": "nominal"},
            "size": {"value": "pointSize"},
        }
    if chart_type == "heatmap":
        return {
            "x": {"field": "metabolite", "type": "nominal"},
            "y": {"field": "module/gene", "type": "nominal"},
            "color": {"field": "association score", "type": "quantitative"},
            "size": None,
        }
    return {"x": None, "y": None, "color": None, "size": None}


def _axis_labels_from_encoding(encoding: dict[str, Any]) -> dict[str, str | None]:
    labels: dict[str, str | None] = {"x": None, "y": None, "color": None, "size": None}
    for axis in ("x", "y", "color", "size"):
        entry = encoding.get(axis)
        if isinstance(entry, dict):
            labels[axis] = entry.get("field") or None
    return labels


def _default_thresholds_for_chart(chart_type: str) -> list[dict[str, Any]]:
    if chart_type == "volcano":
        return [
            {"axis": "x", "value": -1, "label": "log2FC cutoff"},
            {"axis": "x", "value": 1, "label": "log2FC cutoff"},
            {"axis": "y", "value": 1.301, "label": "adjusted p-value cutoff"},
        ]
    if chart_type == "scatter":
        return [{"axis": "y", "value": 0, "label": "zero"}]
    if chart_type == "bar":
        return [{"axis": "x", "value": 1.0, "label": "VIP cutoff"}]
    return []


def _default_sorting_for_chart(chart_type: str) -> dict[str, Any]:
    if chart_type == "heatmap":
        return {"rows": "default", "columns": "significance"}
    if chart_type == "upset":
        return {"sets": "size_desc", "intersections": "size_desc"}
    return {"order": "source"}


def _default_facet_layout_for_chart(chart_type: str) -> dict[str, Any]:
    if chart_type == "pca":
        return {"enabled": False, "columns": 1}
    if chart_type == "heatmap":
        return {"enabled": False, "rowCluster": False, "columnCluster": False}
    return {"enabled": False}


def _infer_data_source_table(stem: str) -> str | None:
    text = stem.lower()
    if "module" in text:
        return "outputs/T09_Module_Metabolite_Association.csv"
    if "network" in text or "circos" in text:
        return "outputs/T03_High_Confidence_Network.csv"
    if "association" in text or "regression" in text:
        return "outputs/T01_Metabolite_Gene_Scoring_Table.csv"
    if "volcano" in text or "deg" in text:
        return "outputs/deg_results.csv"
    if "dem" in text or text.endswith("_all") or text.endswith("_sig"):
        return "outputs/differential_metabolite_counts.csv"
    if "union" in text:
        return "outputs/union_significant_metabolites.csv"
    return None


def _pretty_figure_title(stem: str) -> str:
    return stem.replace("_", " ").replace("-", " ")


def _safe_figure_id(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in value.strip())
    return cleaned.strip("._") or "figure"


def _estimate_job_timing(
    atype: AnalysisType,
    params: JobParams,
    input_paths: dict[str, Path],
) -> dict[str, int | str | None]:
    structured = _estimate_from_input_profile(atype, params, input_paths)
    if structured is None:
        structured = _estimate_from_history(atype)

    if structured is None:
        min_seconds, max_seconds = _default_timing_range(atype)
        return {
            "estimated_total_seconds": None,
            "estimated_remaining_seconds": None,
            "estimated_range_min_seconds": min_seconds,
            "estimated_range_max_seconds": max_seconds,
            "estimated_range_label": _format_duration_range(min_seconds, max_seconds),
        }

    total = int(structured)
    min_seconds = max(60, int(total * 0.65))
    max_seconds = max(min_seconds + 60, int(total * 1.35))
    return {
        "estimated_total_seconds": total,
        "estimated_remaining_seconds": total,
        "estimated_range_min_seconds": min_seconds,
        "estimated_range_max_seconds": max_seconds,
        "estimated_range_label": _format_duration_range(min_seconds, max_seconds),
    }


def _estimate_from_input_profile(
    atype: AnalysisType,
    params: JobParams,
    input_paths: dict[str, Path],
) -> int | None:
    size_mb = sum(path.stat().st_size for path in input_paths.values() if path.exists()) / (1024 * 1024)

    if atype == AnalysisType.DIFFERENTIAL:
        counts_profile = _inspect_csv_profile(input_paths.get("counts"))
        metadata_profile = _inspect_csv_profile(input_paths.get("metadata"))
        if not counts_profile or not metadata_profile:
            return None
        gene_count = counts_profile["rows"]
        sample_count = max(0, counts_profile["columns"] - 1)
        if gene_count <= 0 or sample_count <= 0:
            return None
        estimate = 180 + gene_count * 0.04 + sample_count * 6 + size_mb * 18
        if bool(params.get("normalize", True)):
            estimate += 30
        if bool(params.get("filter_low_expression", True)):
            estimate += 20
        return max(180, min(int(estimate), 3 * 60 * 60))

    if atype == AnalysisType.DEM:
        metab_profile = _inspect_csv_profile(input_paths.get("metabs"))
        metadata_profile = _inspect_csv_profile(input_paths.get("metadata"))
        if not metab_profile or not metadata_profile:
            return None
        metabolite_count = metab_profile["rows"]
        sample_count = max(0, metab_profile["columns"] - 1)
        if metabolite_count <= 0 or sample_count <= 0:
            return None
        estimate = 240 + metabolite_count * 0.06 + sample_count * 8 + size_mb * 22
        if bool(params.get("normalize", True)):
            estimate += 30
        if bool(params.get("log_transform", True)):
            estimate += 20
        tested = str(params.get("tested_levels", "")).split(",")
        n_contrasts = len([t for t in tested if t.strip()])
        estimate += n_contrasts * 120
        return max(240, min(int(estimate), 4 * 60 * 60))

    transcriptome_profile = _inspect_csv_profile(input_paths.get("transcriptome"))
    metabolome_profile = _inspect_csv_profile(input_paths.get("metabolome"))
    group_profile = _inspect_csv_profile(input_paths.get("group"))
    if not transcriptome_profile or not metabolome_profile or not group_profile:
        return None
    gene_count = transcriptome_profile["rows"]
    metabolite_count = metabolome_profile["rows"]
    sample_count = group_profile["rows"]
    if gene_count <= 0 or metabolite_count <= 0 or sample_count <= 0:
        return None
    estimate = 240 + gene_count * 0.025 + metabolite_count * 0.06 + sample_count * 8 + size_mb * 20
    if bool(params.get("enable_modules", True)):
        estimate += 90
    return max(240, min(int(estimate), 4 * 60 * 60))


def _estimate_from_history(atype: AnalysisType) -> int | None:
    durations: list[float] = []
    for job in JOB_STORE.list_internal():
        if job.analysis_type != atype or job.status != JobStatus.SUCCEEDED:
            continue
        if job.started_at and job.completed_at:
            duration = (job.completed_at - job.started_at).total_seconds()
        else:
            duration = (job.updated_at - job.created_at).total_seconds()
        if duration > 0:
            durations.append(duration)

    if not durations:
        return None
    return max(120, int(median(durations)))


def _default_timing_range(atype: AnalysisType) -> tuple[int, int]:
    if atype == AnalysisType.DIFFERENTIAL:
        return 5 * 60, 15 * 60
    if atype == AnalysisType.DEM:
        return 6 * 60, 25 * 60
    return 8 * 60, 25 * 60


def _inspect_csv_profile(path: Path | None) -> dict[str, int] | None:
    if path is None or not path.exists() or not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
            reader = csv.reader(handle)
            header = next(reader, None)
            if header is None:
                return None
            rows = 0
            for row in reader:
                if any(cell.strip() for cell in row):
                    rows += 1
            return {"columns": len(header), "rows": rows}
    except Exception:
        return None


def _format_duration_range(min_seconds: int, max_seconds: int) -> str:
    min_minutes = max(1, round(min_seconds / 60))
    max_minutes = max(min_minutes, round(max_seconds / 60))
    if max_minutes < 60:
        return f"{min_minutes}-{max_minutes} min"
    min_hours = round(min_seconds / 3600, 1)
    max_hours = round(max_seconds / 3600, 1)
    return f"{min_hours}-{max_hours} h"


def _remaining_seconds(job: JobRecord, progress: int) -> int | None:
    total = job.estimated_total_seconds
    if total is None and job.estimated_range_min_seconds is not None and job.estimated_range_max_seconds is not None:
        total = int((job.estimated_range_min_seconds + job.estimated_range_max_seconds) / 2)
    if total is None:
        return job.estimated_remaining_seconds
    return max(0, int(total * (100 - progress) / 100))


def _job_project_id(job: JobRecord) -> str:
    return job.project_id or job.id


def _current_job_timing(job: JobRecord) -> dict[str, int | str | None]:
    now = datetime.now(timezone.utc)
    if job.started_at and job.completed_at:
        elapsed = max(0, int((job.completed_at - job.started_at).total_seconds()))
    elif job.started_at:
        elapsed = max(0, int((now - job.started_at).total_seconds()))
    else:
        elapsed = max(0, int((now - job.created_at).total_seconds()))

    remaining = job.estimated_remaining_seconds
    if job.status in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}:
        remaining = 0
    elif job.status == JobStatus.RUNNING:
        remaining = _remaining_seconds(job, job.progress)
    elif job.status == JobStatus.QUEUED and remaining is None:
        remaining = job.estimated_total_seconds

    return {
        "started_at": job.started_at,
        "completed_at": job.completed_at,
        "estimated_total_seconds": job.estimated_total_seconds,
        "estimated_remaining_seconds": remaining,
        "estimated_range_min_seconds": job.estimated_range_min_seconds,
        "estimated_range_max_seconds": job.estimated_range_max_seconds,
        "elapsed_seconds": elapsed,
        "estimated_range_label": job.estimated_range_label,
    }


def _to_job_progress_response(job: JobRecord) -> JobProgressResponse:
    timing = _current_job_timing(job)
    recent_log_name, recent_log_excerpt = FILES.recent_log(job.id)
    return JobProgressResponse(
        job_id=job.id,
        project_id=_job_project_id(job),
        is_demo=job.is_demo,
        status=job.status,
        progress=job.progress,
        progress_step=job.progress_step,
        error=job.error,
        error_info=analysis_failure_detail(job.error) if job.status == JobStatus.FAILED else None,
        recent_log_name=recent_log_name,
        recent_log_excerpt=recent_log_excerpt,
        **timing,
    )


def _to_job_response(job: JobRecord) -> JobResponse:
    timing = _current_job_timing(job)
    return JobResponse(
        id=job.id,
        project_id=_job_project_id(job),
        project_name=job.project_name,
        analysis_type=job.analysis_type,
        status=job.status,
        is_demo=job.is_demo,
        created_at=job.created_at,
        updated_at=job.updated_at,
        owner_type=job.owner_type,
        owner_id=job.owner_id,
        owner_label=job.owner_label,
        progress=job.progress,
        progress_step=job.progress_step,
        error=job.error,
        error_info=analysis_failure_detail(job.error) if job.status == JobStatus.FAILED else None,
        result_files=job.result_files,
        report_links=job.report_links,
        params=job.params,
        **timing,
    )
