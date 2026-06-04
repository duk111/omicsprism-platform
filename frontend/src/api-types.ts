// Auto-generated from FastAPI /openapi.json. Run `npm run generate-api-types` to refresh.
// Backend: backend/app/main.py Pydantic models

export type AnalysisType = "differential" | "correlation" | "dem";

export type JobStatus = "queued" | "running" | "succeeded" | "failed" | "cancelled";

export interface ResultFileInfo {
  name: string;
  path: string;
  size_bytes: number;
  download_url: string;
}

export interface ReportLinks {
  summary: string | null;
  interactive: string | null;
}

export type ApiErrorCategory =
  | "input_error"
  | "permission_error"
  | "resource_error"
  | "analysis_failed"
  | "system_error";

export interface ApiErrorDetail {
  category: ApiErrorCategory;
  code: string;
  message: string;
  user_message: string;
  suggestions: string[];
  technical_detail: string | null;
  context: Record<string, unknown>;
}

export interface ProjectResponse {
  id: string;
  owner_id: string;
  owner_label: string | null;
  name: string;
  description: string | null;
  is_demo: boolean;
  created_at: string;
  updated_at: string;
  job_count: number;
  queued_jobs: number;
  running_jobs: number;
  succeeded_jobs: number;
  failed_jobs: number;
  cancelled_jobs: number;
  latest_job_at: string | null;
}

export interface ProjectListResponse {
  projects: ProjectResponse[];
}

export interface JobResponse {
  id: string;
  project_id: string | null;
  project_name: string;
  analysis_type: AnalysisType;
  status: JobStatus;
  is_demo: boolean;
  created_at: string;
  updated_at: string;
  owner_type: "user" | "project";
  owner_id: string;
  owner_label: string | null;
  progress: number;
  progress_step: string;
  error: string | null;
  error_info: ApiErrorDetail | null;
  result_files: ResultFileInfo[];
  report_links: ReportLinks;
  params: Record<string, string | number | boolean | null>;
  started_at: string | null;
  completed_at: string | null;
  estimated_total_seconds: number | null;
  estimated_remaining_seconds: number | null;
  estimated_range_min_seconds: number | null;
  estimated_range_max_seconds: number | null;
  elapsed_seconds: number | null;
  estimated_range_label: string | null;
}

export interface JobProgressResponse {
  job_id: string;
  project_id: string | null;
  status: JobStatus;
  is_demo: boolean;
  progress: number;
  progress_step: string;
  error: string | null;
  error_info: ApiErrorDetail | null;
  recent_log_name: string | null;
  recent_log_excerpt: string | null;
  started_at: string | null;
  completed_at: string | null;
  estimated_total_seconds: number | null;
  estimated_remaining_seconds: number | null;
  estimated_range_min_seconds: number | null;
  estimated_range_max_seconds: number | null;
  elapsed_seconds: number | null;
  estimated_range_label: string | null;
}

export interface JobListResponse {
  jobs: JobResponse[];
}

export interface AuditEventRecord {
  id: string;
  event_type: string;
  action: string;
  job_id: string | null;
  user_id: string | null;
  project_id: string | null;
  request_id: string | null;
  status_from: string | null;
  status_to: string | null;
  entity_type: string | null;
  entity_id: string | null;
  message: string | null;
  metadata: Record<string, unknown>;
  created_at: string;
}

export interface MetricsResponse {
  generated_at: string;
  total_jobs: number;
  queued_jobs: number;
  running_jobs: number;
  succeeded_jobs: number;
  failed_jobs: number;
  cancelled_jobs: number;
  failure_rate: number;
  average_duration_seconds: number | null;
  queue_length: number;
  storage_usage_bytes: number;
  audit_event_count: number;
}

export interface QuotaScopeUsage {
  active_jobs: number;
  active_limit: number | null;
  storage_used_bytes: number;
  storage_limit_bytes: number | null;
  storage_available_bytes: number | null;
}

export interface QuotaUsageResponse {
  user: QuotaScopeUsage;
  project: QuotaScopeUsage | null;
  can_submit: boolean;
  reasons: string[];
}

export interface JobControlResponse {
  job: JobResponse;
  message: string;
}

export interface UploadedFileInfo {
  field: string;
  filename: string;
  content_type: string | null;
  size_bytes: number;
  path: string;
}

export interface JobFilesResponse {
  job_id: string;
  inputs: UploadedFileInfo[];
  result_files: ResultFileInfo[];
  report_links: ReportLinks;
}

export interface JobLogResponse {
  job_id: string;
  log_name: string | null;
  content: string;
}

export interface ImageInfo {
  name: string;
  path: string;
  thumbnail_url: string;
  full_url: string;
  interactive_url: string | null;
}

export interface SummaryMetric {
  key: string;
  label: string;
  value: string | number | null;
  unit: string | null;
}

export interface SummaryFigure {
  name: string;
  path: string;
  url: string;
}

export interface SummaryRuntime {
  started_at: string | null;
  completed_at: string | null;
  elapsed_seconds: number | null;
}

export interface SummaryInputFile {
  field: string | null;
  filename: string;
  checksum: string | null;
  content_type: string | null;
  size_bytes: number;
  storage_key: string;
}

export interface ResultSummaryResponse {
  job_id: string;
  project_id: string | null;
  analysis_type: AnalysisType;
  generated_at: string;
  headline: string;
  interpretation: string[];
  metrics: SummaryMetric[];
  top_items: Array<Record<string, string | number | null>>;
  module_associations: Array<Record<string, string | number | null>>;
  main_figures: SummaryFigure[];
  parameters: Record<string, string | number | boolean | null>;
  input_files: SummaryInputFile[];
  runtime: SummaryRuntime;
  software_versions: Record<string, string>;
  warnings: string[];
  exports: Record<string, string>;
}

export interface PreflightIssue {
  code:
    | "invalid_analysis_type"
    | "missing_required_field"
    | "missing_required_columns"
    | "empty_file"
    | "invalid_csv"
    | "matrix_schema_invalid"
    | "group_schema_invalid"
    | "empty_column"
    | "duplicate_feature_id"
    | "duplicate_sample_id"
    | "sample_mismatch"
    | "sample_order_mismatch"
    | "non_numeric_value"
    | "inconsistent_row_length";
  field: string | null;
  severity: "error" | "warning";
  message: string;
  context: Record<string, unknown>;
  suggestions: string[];
}

export interface PreflightFileSummary {
  field: string;
  filename: string;
  rows: number;
  columns: number;
  sample_names: string[];
  sample_ids: string[];
  feature_ids: string[];
  duplicate_ids: string[];
  empty_columns: string[];
  required_columns: string[];
  non_numeric_cells: number;
  row_length_issues: number;
}

export interface PreflightResponse {
  analysis_type: AnalysisType;
  ok: boolean;
  can_submit: boolean;
  normalized_params: Record<string, string | number | boolean | null>;
  files: PreflightFileSummary[];
  errors: PreflightIssue[];
  warnings: PreflightIssue[];
}

export interface AnalysisGuideFile {
  field: string;
  label: string;
  description: string;
  template: string;
  example_filename: string;
}

export interface AnalysisGuideParameter {
  name: string;
  label: string;
  description: string;
  example: string | null;
}

export interface AnalysisGuideResponse {
  analysis_type: AnalysisType;
  title: string;
  summary: string;
  notes: string[];
  required_files: AnalysisGuideFile[];
  parameters: AnalysisGuideParameter[];
  demo_notes: string[];
}

export interface DemoJobRequest {
  analysis_type: AnalysisType;
}

export type FigureControlValue =
  | string
  | number
  | boolean
  | string[]
  | number[]
  | Record<string, string | number | boolean | null>
  | null;

export interface FigureSpec {
  schemaVersion: string;
  figureId: string;
  title: string;
  chartType: string;
  interactiveMode: "full" | "partial";
  sourceStaticImagePaths: {
    png: string | null;
    svg: string | null;
    pdf: string | null;
  };
  dataSourceTablePath: string | null;
  encoding: Record<string, unknown>;
  xEncoding: Record<string, unknown> | null;
  yEncoding: Record<string, unknown> | null;
  colorEncoding: Record<string, unknown> | null;
  sizeEncoding: Record<string, unknown> | null;
  labels: Record<string, string | null>;
  axisRange: Record<string, unknown>;
  legendOrder: string[];
  palette: {
    categorical: string[];
    continuous: string[];
    single: Record<string, string>;
    active: string;
  };
  thresholds: Array<Record<string, unknown>>;
  sorting: Record<string, unknown>;
  facetLayout: Record<string, unknown>;
  defaultControls: Record<string, FigureControlValue>;
  controls: Record<string, FigureControlValue>;
  allowedControls: Array<Record<string, unknown>>;
  statistics: Record<string, unknown>;
  annotations: Array<Record<string, unknown>>;
  provenance: Record<string, unknown>;
}

export interface FigureManifestResponse {
  job_id: string;
  figures: Array<{
    figureId: string;
    title: string;
    chartType: string;
    interactiveMode: "full" | "partial";
    specPath: string;
    thumbnailUrl: string;
  }>;
}
