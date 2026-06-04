import type { JobProgressResponse } from "../../api-types";

export type ProgressConnectionMode = "sse" | "polling";
export type ProgressConnectionState = "connecting" | "open" | "recovering" | "fallback" | "closed";

export const TERMINAL_JOB_STATUSES = new Set(["succeeded", "failed", "cancelled"]);

export function isTerminalProgress(progress: JobProgressResponse | null) {
  return progress !== null && TERMINAL_JOB_STATUSES.has(progress.status);
}

export async function fetchJobProgress(jobId: string, signal?: AbortSignal): Promise<JobProgressResponse> {
  const response = await fetch(`/api/jobs/${jobId}/progress`, { signal });
  const data = await response.json().catch(() => null);
  if (!response.ok) throw new Error(data?.detail ?? "Failed to load job progress");
  return data as JobProgressResponse;
}

export function formatSeconds(totalSeconds: number | null | undefined) {
  if (totalSeconds === null || totalSeconds === undefined) return "Estimating";
  if (totalSeconds <= 0) return "Done";
  const seconds = Math.max(0, Math.round(totalSeconds));
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);
  if (hours > 0) return `${hours}h ${minutes % 60}m`;
  if (minutes > 0) return `${minutes}m ${seconds % 60}s`;
  return `${seconds}s`;
}
