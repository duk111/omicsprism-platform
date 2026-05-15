import { useEffect, useState } from "react";
import type { AnalysisType, JobStatus, JobResponse, ResultFileInfo, ReportLinks } from "../api-types";
import "./JobListPanel.css";

const statusLabels: Record<JobStatus, string> = {
  queued: "排队中",
  running: "运行中",
  succeeded: "已完成",
  failed: "失败"
};

const analysisLabels: Record<string, string> = {
  differential: "差异基因分析",
  correlation: "关联分析"
};

interface Props {
  onSelectJob: (jobId: string) => void;
  selectedJobId: string | null;
}

export default function JobListPanel({ onSelectJob, selectedJobId }: Props) {
  const [jobs, setJobs] = useState<JobResponse[]>([]);
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    void loadJobs();
    const timer = window.setInterval(() => loadJobs(true), 5000);
    return () => window.clearInterval(timer);
  }, []);

  async function loadJobs(quiet = false) {
    try {
      const res = await fetch("/api/jobs");
      if (!res.ok) throw new Error("Failed");
      const data = await res.json();
      const sorted = data.jobs.sort(
        (a: JobResponse, b: JobResponse) =>
          new Date(b.created_at).getTime() - new Date(a.created_at).getTime()
      );
      setJobs(sorted);
    } catch {
      if (!quiet) console.error("Failed to load jobs");
    }
  }

  return (
    <div className={`job-panel ${expanded ? "expanded" : ""}`}>
      <button
        className="job-panel-toggle"
        type="button"
        onClick={() => setExpanded((v) => !v)}
      >
        {expanded ? "收起任务列表" : "展开任务列表"} ({jobs.length})
      </button>
      {expanded && (
        <div className="job-list">
          {jobs.length === 0 && <p className="job-empty">暂无任务</p>}
          {jobs.map((job) => (
            <button
              key={job.id}
              className={`job-item ${selectedJobId === job.id ? "selected" : ""}`}
              type="button"
              onClick={() => onSelectJob(job.id)}
            >
              <span className={`status-dot status-${job.status}`} />
              <span className="job-info">
                <strong>
                  {job.project_name}
                  <small>{analysisLabels[job.analysis_type] ?? job.analysis_type}</small>
                </strong>
                <span className="job-meta">
                  {new Date(job.created_at).toLocaleString()}
                </span>
              </span>
              <em>{statusLabels[job.status]}</em>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
