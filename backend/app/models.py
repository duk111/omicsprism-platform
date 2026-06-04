from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

from .errors import ApiErrorDetail


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AnalysisType(str, Enum):
    DIFFERENTIAL = "differential"
    CORRELATION = "correlation"
    DEM = "dem"


class FileArtifactKind(str, Enum):
    INPUT = "input"
    OUTPUT = "output"
    REPORT = "report"
    IMAGE = "image"
    LOG = "log"
    FIGURE = "figure"
    TEMP = "temp"


class FileArtifactInfo(BaseModel):
    kind: FileArtifactKind
    field: str | None = None
    filename: str
    path: str
    storage_key: str
    checksum: str | None = None
    content_type: str | None = None
    size_bytes: int
    created_at: datetime


class ImageInfo(FileArtifactInfo):
    kind: FileArtifactKind = FileArtifactKind.IMAGE
    name: str
    thumbnail_url: str
    full_url: str
    interactive_url: str | None = None


class UploadedFileInfo(FileArtifactInfo):
    kind: FileArtifactKind = FileArtifactKind.INPUT
    field: str


class ResultFileInfo(FileArtifactInfo):
    kind: FileArtifactKind = FileArtifactKind.OUTPUT
    name: str
    download_url: str


class ReportLinks(BaseModel):
    summary: str | None = None
    interactive: str | None = None


class JobTimingInfo(BaseModel):
    started_at: datetime | None = None
    completed_at: datetime | None = None
    estimated_total_seconds: int | None = None
    estimated_remaining_seconds: int | None = None
    estimated_range_min_seconds: int | None = None
    estimated_range_max_seconds: int | None = None
    elapsed_seconds: int | None = None
    estimated_range_label: str | None = None


JobParams = dict[str, str | int | float | bool | None]


class UserRecord(BaseModel):
    id: str
    email: str
    display_name: str | None = None
    created_at: datetime
    updated_at: datetime
    is_active: bool = True


class CurrentUserResponse(BaseModel):
    id: str
    email: str
    display_name: str | None = None


class LoginRequest(BaseModel):
    email: str
    password: str


class RegisterRequest(LoginRequest):
    display_name: str | None = None


class AuthResponse(BaseModel):
    user: CurrentUserResponse
    access_token: str
    token_type: str = "bearer"


class ProjectRecord(BaseModel):
    id: str
    owner_id: str
    owner_label: str | None = None
    name: str
    description: str | None = None
    is_demo: bool = False
    created_at: datetime
    updated_at: datetime


class ProjectCreateRequest(BaseModel):
    name: str
    description: str | None = None


class ProjectResponse(BaseModel):
    id: str
    owner_id: str
    owner_label: str | None = None
    name: str
    description: str | None = None
    is_demo: bool = False
    created_at: datetime
    updated_at: datetime
    job_count: int = 0
    queued_jobs: int = 0
    running_jobs: int = 0
    succeeded_jobs: int = 0
    failed_jobs: int = 0
    cancelled_jobs: int = 0
    latest_job_at: datetime | None = None


class ProjectListResponse(BaseModel):
    projects: list[ProjectResponse]


class JobOwnerType(str, Enum):
    USER = "user"
    PROJECT = "project"


class JobRecord(JobTimingInfo):
    id: str
    project_id: str | None = None
    project_name: str
    analysis_type: AnalysisType
    status: JobStatus
    is_demo: bool = False
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None
    owner_type: JobOwnerType = JobOwnerType.USER
    owner_id: str = ""
    owner_label: str | None = None
    inputs: list[UploadedFileInfo] = Field(default_factory=list)
    result_files: list[ResultFileInfo] = Field(default_factory=list)
    report_links: ReportLinks = Field(default_factory=ReportLinks)
    artifacts: list[FileArtifactInfo] = Field(default_factory=list)
    progress: int = 0
    progress_step: str = ""
    error: str | None = None
    params: JobParams = Field(default_factory=dict)
    attempt: int = 0
    max_retries: int = 0


class JobResponse(JobTimingInfo):
    id: str
    project_id: str | None = None
    project_name: str
    analysis_type: AnalysisType
    status: JobStatus
    is_demo: bool = False
    created_at: datetime
    updated_at: datetime
    owner_type: JobOwnerType = JobOwnerType.USER
    owner_id: str = ""
    owner_label: str | None = None
    progress: int = 0
    progress_step: str = ""
    error: str | None = None
    result_files: list[ResultFileInfo] = Field(default_factory=list)
    report_links: ReportLinks = Field(default_factory=ReportLinks)
    params: JobParams = Field(default_factory=dict)
    attempt: int = 0
    max_retries: int = 0
    error_info: ApiErrorDetail | None = None


class JobProgressResponse(JobTimingInfo):
    job_id: str
    project_id: str | None = None
    status: JobStatus
    is_demo: bool = False
    progress: int = 0
    progress_step: str = ""
    error: str | None = None
    error_info: ApiErrorDetail | None = None
    recent_log_name: str | None = None
    recent_log_excerpt: str | None = None


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


class SummaryMetric(BaseModel):
    key: str
    label: str
    value: str | int | float | None
    unit: str | None = None


class SummaryFigure(BaseModel):
    name: str
    path: str
    url: str


class SummaryRuntime(BaseModel):
    started_at: datetime | None = None
    completed_at: datetime | None = None
    elapsed_seconds: int | None = None


class SummaryInputFile(BaseModel):
    field: str | None = None
    filename: str
    checksum: str | None = None
    content_type: str | None = None
    size_bytes: int
    storage_key: str


class ResultSummaryResponse(BaseModel):
    job_id: str
    project_id: str | None = None
    analysis_type: AnalysisType
    generated_at: datetime
    headline: str
    interpretation: list[str] = Field(default_factory=list)
    metrics: list[SummaryMetric] = Field(default_factory=list)
    top_items: list[dict[str, Any]] = Field(default_factory=list)
    module_associations: list[dict[str, Any]] = Field(default_factory=list)
    main_figures: list[SummaryFigure] = Field(default_factory=list)
    parameters: JobParams = Field(default_factory=dict)
    input_files: list[SummaryInputFile] = Field(default_factory=list)
    runtime: SummaryRuntime = Field(default_factory=SummaryRuntime)
    software_versions: dict[str, str] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    exports: dict[str, str] = Field(default_factory=dict)


class AuditEventRecord(BaseModel):
    id: str
    event_type: str
    action: str
    job_id: str | None = None
    user_id: str | None = None
    project_id: str | None = None
    request_id: str | None = None
    status_from: str | None = None
    status_to: str | None = None
    entity_type: str | None = None
    entity_id: str | None = None
    message: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class AuditEventsResponse(BaseModel):
    events: list[AuditEventRecord]


class MetricsResponse(BaseModel):
    generated_at: datetime
    total_jobs: int
    queued_jobs: int
    running_jobs: int
    succeeded_jobs: int
    failed_jobs: int
    cancelled_jobs: int
    failure_rate: float
    average_duration_seconds: float | None
    queue_length: int
    storage_usage_bytes: int
    audit_event_count: int


class FigureManifestResponse(BaseModel):
    job_id: str
    figures: list[dict[str, Any]]


class PreflightIssueCode(str, Enum):
    INVALID_ANALYSIS_TYPE = "invalid_analysis_type"
    MISSING_REQUIRED_FIELD = "missing_required_field"
    MISSING_REQUIRED_COLUMNS = "missing_required_columns"
    EMPTY_FILE = "empty_file"
    INVALID_CSV = "invalid_csv"
    MATRIX_SCHEMA_INVALID = "matrix_schema_invalid"
    GROUP_SCHEMA_INVALID = "group_schema_invalid"
    EMPTY_COLUMN = "empty_column"
    DUPLICATE_FEATURE_ID = "duplicate_feature_id"
    DUPLICATE_SAMPLE_ID = "duplicate_sample_id"
    SAMPLE_MISMATCH = "sample_mismatch"
    SAMPLE_ORDER_MISMATCH = "sample_order_mismatch"
    NON_NUMERIC_VALUE = "non_numeric_value"
    INCONSISTENT_ROW_LENGTH = "inconsistent_row_length"


class PreflightIssue(BaseModel):
    code: PreflightIssueCode
    field: str | None = None
    severity: Literal["error", "warning"] = "error"
    message: str
    context: dict[str, Any] = Field(default_factory=dict)
    suggestions: list[str] = Field(default_factory=list)


class PreflightFileSummary(BaseModel):
    field: str
    filename: str
    rows: int = 0
    columns: int = 0
    sample_names: list[str] = Field(default_factory=list)
    sample_ids: list[str] = Field(default_factory=list)
    feature_ids: list[str] = Field(default_factory=list)
    duplicate_ids: list[str] = Field(default_factory=list)
    empty_columns: list[str] = Field(default_factory=list)
    required_columns: list[str] = Field(default_factory=list)
    non_numeric_cells: int = 0
    row_length_issues: int = 0


class PreflightResponse(BaseModel):
    analysis_type: AnalysisType
    ok: bool
    can_submit: bool
    normalized_params: JobParams = Field(default_factory=dict)
    files: list[PreflightFileSummary] = Field(default_factory=list)
    errors: list[PreflightIssue] = Field(default_factory=list)
    warnings: list[PreflightIssue] = Field(default_factory=list)


class AnalysisGuideFile(BaseModel):
    field: str
    label: str
    description: str
    template: str
    example_filename: str


class AnalysisGuideParameter(BaseModel):
    name: str
    label: str
    description: str
    example: str | None = None


class AnalysisGuideResponse(BaseModel):
    analysis_type: AnalysisType
    title: str
    summary: str
    notes: list[str] = Field(default_factory=list)
    required_files: list[AnalysisGuideFile] = Field(default_factory=list)
    parameters: list[AnalysisGuideParameter] = Field(default_factory=list)
    demo_notes: list[str] = Field(default_factory=list)


class DemoJobRequest(BaseModel):
    analysis_type: AnalysisType


class JobControlResponse(BaseModel):
    job: JobResponse
    message: str


class QuotaScopeUsage(BaseModel):
    active_jobs: int
    active_limit: int | None = None
    storage_used_bytes: int
    storage_limit_bytes: int | None = None
    storage_available_bytes: int | None = None


class QuotaUsageResponse(BaseModel):
    user: QuotaScopeUsage
    project: QuotaScopeUsage | None = None
    can_submit: bool
    reasons: list[str] = Field(default_factory=list)
