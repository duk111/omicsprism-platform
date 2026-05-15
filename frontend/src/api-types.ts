// Auto-generated from FastAPI /openapi.json — run `npm run generate-api-types` to refresh
// Backend: backend/app/main.py Pydantic models

export type AnalysisType = "differential" | "correlation";

export type JobStatus = "queued" | "running" | "succeeded" | "failed";

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

export interface JobResponse {
  id: string;
  project_name: string;
  analysis_type: AnalysisType;
  status: JobStatus;
  created_at: string;
  updated_at: string;
  progress: number;
  progress_step: string;
  error: string | null;
  result_files: ResultFileInfo[];
  report_links: ReportLinks;
}

export interface JobListResponse {
  jobs: JobResponse[];
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
}
