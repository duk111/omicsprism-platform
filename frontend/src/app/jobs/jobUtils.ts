import type { AnalysisType, JobResponse, JobStatus } from "../../api-types";

export const STATUS_LABELS: Record<JobStatus, string> = {
  queued: "Queued",
  running: "Running",
  succeeded: "Succeeded",
  failed: "Failed",
  cancelled: "Cancelled",
};

export const ANALYSIS_LABELS: Record<AnalysisType, string> = {
  deg: "DEG",
  dem: "DEM",
  gma: "GMA",
};

export type JobStatusFilter = "all" | JobStatus;
export type JobSortKey = "created_desc" | "created_asc" | "updated_desc" | "duration_desc" | "project_asc";

export function jobSearchText(job: JobResponse) {
  return [
    job.project_name,
    job.id,
    job.analysis_type,
    STATUS_LABELS[job.status],
    job.progress_step,
    job.owner_label,
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
}

export function compareJobs(a: JobResponse, b: JobResponse, sortKey: JobSortKey) {
  if (sortKey === "created_asc") return dateMs(a.created_at) - dateMs(b.created_at);
  if (sortKey === "updated_desc") return dateMs(b.updated_at) - dateMs(a.updated_at);
  if (sortKey === "duration_desc") return durationSeconds(b) - durationSeconds(a);
  if (sortKey === "project_asc") return a.project_name.localeCompare(b.project_name);
  return dateMs(b.created_at) - dateMs(a.created_at);
}

export function dateMs(value: string | null) {
  if (!value) return 0;
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? 0 : parsed;
}

export function durationSeconds(job: JobResponse) {
  if (job.elapsed_seconds != null) return job.elapsed_seconds;
  if (job.started_at && job.status === "running") return Math.max(0, Math.round((Date.now() - dateMs(job.started_at)) / 1000));
  if (job.started_at && job.completed_at) return Math.max(0, Math.round((dateMs(job.completed_at) - dateMs(job.started_at)) / 1000));
  return 0;
}

export function formatJobDuration(job: JobResponse) {
  const seconds = durationSeconds(job);
  if (!seconds && job.status === "queued") return "Queued";
  if (!seconds) return "Less than 1s";
  return formatDuration(seconds);
}

export function formatDuration(totalSeconds: number) {
  const seconds = Math.max(0, Math.round(totalSeconds));
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);
  if (hours > 0) return `${hours}h ${minutes % 60}m`;
  if (minutes > 0) return `${minutes}m ${seconds % 60}s`;
  return `${seconds}s`;
}

export function formatDateTime(value: string | null) {
  if (!value) return "Never";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleString(undefined, {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}
