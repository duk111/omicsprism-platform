from __future__ import annotations

import traceback
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from omicsprism.config import AnalysisConfig
from omicsprism.core import MultiOmicsEngine
from omicsprism.io import load_as_anndata, preprocess_adata
from omicsprism.utils import get_logger
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class AnalysisType(str, Enum):
    DIFFERENTIAL = "differential"
    CORRELATION = "correlation"


class ImageInfo(BaseModel):
    name: str
    path: str
    thumbnail_url: str
    full_url: str


class UploadedFileInfo(BaseModel):
    field: str
    filename: str
    content_type: str | None = None
    size_bytes: int
    path: str


class ResultFileInfo(BaseModel):
    name: str
    path: str
    size_bytes: int
    download_url: str


class ReportLinks(BaseModel):
    summary: str | None = None
    interactive: str | None = None


class JobRecord(BaseModel):
    id: str
    project_name: str
    analysis_type: AnalysisType
    status: JobStatus
    created_at: datetime
    updated_at: datetime
    inputs: list[UploadedFileInfo] = Field(default_factory=list)
    result_files: list[ResultFileInfo] = Field(default_factory=list)
    report_links: ReportLinks = Field(default_factory=ReportLinks)
    progress: int = 0
    progress_step: str = ""
    error: str | None = None


class JobResponse(BaseModel):
    id: str
    project_name: str
    analysis_type: AnalysisType
    status: JobStatus
    created_at: datetime
    updated_at: datetime
    progress: int = 0
    progress_step: str = ""
    error: str | None = None
    result_files: list[ResultFileInfo] = Field(default_factory=list)
    report_links: ReportLinks = Field(default_factory=ReportLinks)


class JobFilesResponse(BaseModel):
    job_id: str
    inputs: list[UploadedFileInfo]
    result_files: list[ResultFileInfo]
    report_links: ReportLinks = Field(default_factory=ReportLinks)


class JobListResponse(BaseModel):
    jobs: list[JobResponse]


class JobLogResponse(BaseModel):
    job_id: str
    log_name: str | None = None
    content: str = ""


BASE_DIR = Path(__file__).resolve().parents[2]
RUNS_DIR = BASE_DIR / "runs"
EXECUTOR = ThreadPoolExecutor(max_workers=1)

app = FastAPI(title="OmicsPrism Platform API", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/jobs", response_model=JobResponse, status_code=201)
async def create_job(
    project_name: Annotated[str, Form(min_length=1)],
    analysis_type: Annotated[str, Form()],
    transcriptome: Annotated[UploadFile | None, File()] = None,
    metabolome: Annotated[UploadFile | None, File()] = None,
    group: Annotated[UploadFile | None, File()] = None,
) -> JobResponse:
    try:
        atype = AnalysisType(analysis_type)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Unknown analysis_type: {analysis_type}")

    if atype == AnalysisType.CORRELATION:
        if transcriptome is None:
            raise HTTPException(status_code=400, detail="transcriptome is required for correlation analysis")
        if metabolome is None:
            raise HTTPException(status_code=400, detail="metabolome is required for correlation analysis")
        if group is None:
            raise HTTPException(status_code=400, detail="group is required for correlation analysis")
    else:
        raise HTTPException(status_code=400, detail=f"Unknown analysis_type: {analysis_type}")

    job_id = str(uuid4())
    run_dir = _run_dir(job_id)
    input_dir = run_dir / "inputs"
    output_dir = run_dir / "outputs"
    input_dir.mkdir(parents=True, exist_ok=False)
    output_dir.mkdir(parents=True, exist_ok=True)

    inputs: list[UploadedFileInfo] = []
    inputs.append(await _save_upload("transcriptome", transcriptome, input_dir, "transcriptome.csv"))  # type: ignore[arg-type]
    inputs.append(await _save_upload("metabolome", metabolome, input_dir, "metabolome.csv"))  # type: ignore[arg-type]
    inputs.append(await _save_upload("group", group, input_dir, "group.csv"))  # type: ignore[arg-type]

    now = datetime.now(UTC)
    job = JobRecord(
        id=job_id,
        project_name=project_name.strip(),
        analysis_type=atype,
        status=JobStatus.QUEUED,
        created_at=now,
        updated_at=now,
        inputs=inputs,
    )
    _write_job(job)

    EXECUTOR.submit(_run_omicsprism_job, job_id)
    return _to_job_response(job)


@app.get("/api/jobs", response_model=JobListResponse)
def list_jobs() -> JobListResponse:
    if not RUNS_DIR.exists():
        return JobListResponse(jobs=[])

    jobs = []
    for path in RUNS_DIR.glob("*/job.json"):
        try:
            jobs.append(_to_job_response(JobRecord.model_validate_json(path.read_text(encoding="utf-8"))))
        except Exception:
            continue
    jobs.sort(key=lambda item: item.created_at, reverse=True)
    return JobListResponse(jobs=jobs)


@app.get("/api/jobs/{job_id}", response_model=JobResponse)
def get_job(job_id: str) -> JobResponse:
    return _to_job_response(_read_job(job_id))


@app.get("/api/jobs/{job_id}/files", response_model=JobFilesResponse)
def get_job_files(job_id: str) -> JobFilesResponse:
    job = _read_job(job_id)
    return JobFilesResponse(
        job_id=job.id,
        inputs=job.inputs,
        result_files=job.result_files,
        report_links=job.report_links,
    )


@app.get("/api/jobs/{job_id}/logs", response_model=JobLogResponse)
def get_job_logs(job_id: str) -> JobLogResponse:
    _read_job(job_id)
    output_dir = _run_dir(job_id) / "outputs"
    candidates = [
        output_dir / "omicsprism.log",
        output_dir / "error.log",
    ]
    for path in candidates:
        if path.exists() and path.is_file():
            content = path.read_text(encoding="utf-8", errors="replace")
            return JobLogResponse(job_id=job_id, log_name=path.name, content=content[-50000:])
    return JobLogResponse(job_id=job_id)


@app.get("/api/jobs/{job_id}/download/{relative_path:path}")
def download_job_file(job_id: str, relative_path: str) -> FileResponse:
    path = _resolve_run_file(job_id, relative_path)
    return FileResponse(path, filename=path.name)


@app.get("/api/jobs/{job_id}/reports/summary")
def open_summary_report(job_id: str) -> FileResponse:
    path = _resolve_run_file(job_id, "outputs/OmicsPrism_Report.html")
    return FileResponse(path, media_type="text/html")


@app.get("/api/jobs/{job_id}/reports/interactive")
def open_interactive_report(job_id: str) -> FileResponse:
    path = _resolve_run_file(job_id, "outputs/OmicsPrism_Interactive_Report.html")
    return FileResponse(path, media_type="text/html")


@app.get("/api/jobs/{job_id}/progress")
def get_job_progress(job_id: str) -> dict[str, int | str]:
    job = _read_job(job_id)
    return {"progress": job.progress, "step": job.progress_step}


@app.get("/api/jobs/{job_id}/images")
def list_job_images(job_id: str) -> list[ImageInfo]:
    _read_job(job_id)
    output_dir = _run_dir(job_id) / "outputs"
    if not output_dir.exists():
        return []

    images: list[ImageInfo] = []
    seen_names: set[str] = set()
    for ext in ("*.png", "*.svg", "*.jpg", "*.jpeg"):
        for path in sorted(output_dir.rglob(ext)):
            stem = path.stem
            if stem in seen_names:
                continue
            seen_names.add(stem)
            relative = path.relative_to(_run_dir(job_id)).as_posix()
            images.append(ImageInfo(
                name=path.name,
                path=relative,
                thumbnail_url=f"/api/jobs/{job_id}/download/{relative}",
                full_url=f"/api/jobs/{job_id}/download/{relative}",
            ))
    return images


async def _save_upload(
    field: str,
    upload: UploadFile,
    input_dir: Path,
    fallback_name: str,
) -> UploadedFileInfo:
    original_filename = _safe_filename(upload.filename, fallback_name)
    if Path(original_filename).suffix.lower() != ".csv":
        raise HTTPException(status_code=400, detail=f"{field} must be a CSV file")

    target_path = input_dir / fallback_name
    with target_path.open("wb") as handle:
        while chunk := await upload.read(1024 * 1024):
            handle.write(chunk)

    size = target_path.stat().st_size
    if size == 0:
        raise HTTPException(status_code=400, detail=f"{field} file is empty")

    max_bytes = 50 * 1024 * 1024  # 50 MB
    if size > max_bytes:
        target_path.unlink()
        raise HTTPException(status_code=413, detail=f"{field} exceeds 50 MB limit ({size / 1024 / 1024:.1f} MB)")

    return UploadedFileInfo(
        field=field,
        filename=original_filename,
        content_type=upload.content_type,
        size_bytes=size,
        path=str(target_path.relative_to(_run_dir(input_dir.parent.name))).replace("\\", "/"),
    )


def _run_omicsprism_job(job_id: str) -> None:
    job = _read_job(job_id)
    run_dir = _run_dir(job_id)
    input_dir = run_dir / "inputs"
    output_dir = run_dir / "outputs"

    def _report(progress: int, step: str) -> None:
        j = _read_job(job_id)
        j.progress = progress
        j.progress_step = step
        _write_job(j)

    try:
        _report(5, "准备数据")
        _update_job(job, status=JobStatus.RUNNING, error=None)

        paths_by_field = {
            file.field: _resolve_run_file(job_id, file.path)
            for file in job.inputs
        }

        group_path = paths_by_field["group"]
        cfg = AnalysisConfig(
            project_name=job.project_name,
            output_dir=str(output_dir),
            group_table_path=str(group_path),
            report_formats=("html",),
            generate_reports=True,
            save_h5ad=True,
            export_cytoscape=True,
        )

        logger = get_logger(log_file=output_dir / "omicsprism.log", level=cfg.log_level)
        logger.info("Launching OmicsPrism platform job: %s", job.id)
        logger.info("Input directory: %s", input_dir.resolve())

        _report(20, "读取数据文件")
        adata = load_as_anndata(
            paths_by_field["transcriptome"],
            paths_by_field["metabolome"],
            group_table_path=cfg.group_table_path,
        )
        _report(40, "数据预处理")
        adata = preprocess_adata(
            adata,
            missing_feature_threshold=cfg.missing_feature_threshold,
            knn_neighbors=cfg.knn_neighbors,
            trans_log2=cfg.trans_log2,
        )
        _report(60, "执行分析计算")
        engine = MultiOmicsEngine(adata, cfg)
        engine.run_all(generate_plots=cfg.generate_reports)
        _report(85, "生成可视化图表")
        _zip_directory(output_dir, output_dir / "OmicsPrism_results.zip")

        _report(100, "分析完成")
        completed = _read_job(job_id)
        _update_job(
            completed,
            status=JobStatus.SUCCEEDED,
            result_files=_collect_result_files(job_id),
            report_links=_collect_report_links(job_id),
            error=None,
        )
    except Exception as exc:  # pragma: no cover - exercised through integration/manual runs
        failed = _read_job(job_id)
        error_path = output_dir / "error.log"
        error_path.write_text(traceback.format_exc(), encoding="utf-8")
        _update_job(failed, status=JobStatus.FAILED, error=str(exc))


def _safe_filename(name: str | None, fallback: str) -> str:
    cleaned = Path(str(name or fallback)).name.strip().replace("\x00", "")
    return cleaned or fallback


def _run_dir(job_id: str) -> Path:
    return RUNS_DIR / job_id


def _job_path(job_id: str) -> Path:
    return _run_dir(job_id) / "job.json"


def _read_job(job_id: str) -> JobRecord:
    path = _job_path(job_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Job not found")
    return JobRecord.model_validate_json(path.read_text(encoding="utf-8"))


def _write_job(job: JobRecord) -> None:
    path = _job_path(job.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(".json.tmp")
    temp_path.write_text(
        job.model_dump_json(indent=2),
        encoding="utf-8",
    )
    temp_path.replace(path)


def _update_job(
    job: JobRecord,
    *,
    status: JobStatus,
    result_files: list[ResultFileInfo] | None = None,
    report_links: ReportLinks | None = None,
    error: str | None = None,
) -> None:
    job.status = status
    job.updated_at = datetime.now(UTC)
    job.error = error
    if result_files is not None:
        job.result_files = result_files
    if report_links is not None:
        job.report_links = report_links
    _write_job(job)


def _collect_result_files(job_id: str) -> list[ResultFileInfo]:
    output_dir = _run_dir(job_id) / "outputs"
    if not output_dir.exists():
        return []

    files: list[ResultFileInfo] = []
    for path in sorted(output_dir.rglob("*")):
        if not path.is_file():
            continue
        relative_path = path.relative_to(_run_dir(job_id)).as_posix()
        files.append(
            ResultFileInfo(
                name=path.name,
                path=relative_path,
                size_bytes=path.stat().st_size,
                download_url=f"/api/jobs/{job_id}/download/{relative_path}",
            )
        )
    return files


def _collect_report_links(job_id: str) -> ReportLinks:
    output_dir = _run_dir(job_id) / "outputs"
    return ReportLinks(
        summary=(
            f"/api/jobs/{job_id}/reports/summary"
            if (output_dir / "OmicsPrism_Report.html").exists()
            else None
        ),
        interactive=(
            f"/api/jobs/{job_id}/reports/interactive"
            if (output_dir / "OmicsPrism_Interactive_Report.html").exists()
            else None
        ),
    )


def _zip_directory(source_dir: Path, zip_path: Path) -> Path:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in source_dir.rglob("*"):
            if path == zip_path or not path.is_file():
                continue
            archive.write(path, path.relative_to(source_dir))
    return zip_path


def _resolve_run_file(job_id: str, relative_path: str) -> Path:
    run_dir = _run_dir(job_id).resolve()
    target = (run_dir / relative_path).resolve()
    if run_dir not in target.parents and target != run_dir:
        raise HTTPException(status_code=400, detail="Invalid file path")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return target


def _to_job_response(job: JobRecord) -> JobResponse:
    return JobResponse(
        id=job.id,
        project_name=job.project_name,
        analysis_type=job.analysis_type,
        status=job.status,
        created_at=job.created_at,
        updated_at=job.updated_at,
        progress=job.progress,
        progress_step=job.progress_step,
        error=job.error,
        result_files=job.result_files,
        report_links=job.report_links,
    )
