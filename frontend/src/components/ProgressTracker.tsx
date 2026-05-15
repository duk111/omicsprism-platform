import { useEffect, useState, useRef, useCallback } from "react";
import type { JobStatus } from "../api-types";
import "./ProgressTracker.css";

const STEPS = ["准备数据", "读取数据文件", "数据预处理", "执行分析计算", "生成可视化图表", "分析完成"];

interface Props {
  jobId: string;
  onBack: () => void;
}

interface ProgressData {
  progress: number;
  step: string;
  status: string;
  error: string | null;
}

export default function ProgressTracker({ jobId, onBack }: Props) {
  const [data, setData] = useState<ProgressData>({ progress: 0, step: "", status: "queued", error: null });
  const pollRef = useRef<number | null>(null);

  const poll = useCallback(async () => {
    try {
      const [progRes, jobRes] = await Promise.all([
        fetch(`/api/jobs/${jobId}/progress`),
        fetch(`/api/jobs/${jobId}`)
      ]);
      if (progRes.ok && jobRes.ok) {
        const prog = await progRes.json();
        const job = await jobRes.json();
        setData({ progress: prog.progress, step: prog.step, status: job.status, error: job.error ?? null });
        if (job.status === "succeeded" || job.status === "failed") {
          return;
        }
      }
    } catch {
      // keep polling
    }
    pollRef.current = window.setTimeout(poll, 2000);
  }, [jobId]);

  useEffect(() => {
    poll();
    return () => {
      if (pollRef.current !== null) window.clearTimeout(pollRef.current);
    };
  }, [poll]);

  const currentStepIdx = STEPS.findIndex((s) => s === data.step);
  const isComplete = data.status === "succeeded";
  const isFailed = data.status === "failed";
  const viewerUrl = `${window.location.origin}${window.location.pathname}?view=results&jobId=${jobId}`;

  return (
    <div className="progress-container">
      <button className="back-button" type="button" onClick={onBack}>
        &larr; 返回
      </button>
      <h2 className="progress-heading">分析进度</h2>

      <div className="progress-bar-track">
        <div
          className={`progress-bar-fill ${isComplete ? "complete" : ""} ${isFailed ? "failed" : ""}`}
          style={{ width: `${Math.max(data.progress, 3)}%` }}
        />
      </div>
      <p className="progress-pct">{data.progress}%</p>

      {isFailed && (
        <div className="progress-error">
          <p className="progress-error-title">分析失败</p>
          {data.error && <pre className="progress-error-detail">{data.error}</pre>}
        </div>
      )}

      {!isFailed && (
        <ol className="step-list">
          {STEPS.map((step, i) => {
            let cls = "step-item";
            if (i < currentStepIdx || (i === currentStepIdx && isComplete)) cls += " done";
            else if (i === currentStepIdx) cls += " active";
            return (
              <li key={step} className={cls}>
                <span className="step-marker">
                  {i < currentStepIdx || (i === currentStepIdx && isComplete) ? "✓" : i + 1}
                </span>
                <span className="step-label">{step}</span>
              </li>
            );
          })}
        </ol>
      )}

      {isComplete && (
        <a className="view-results-button" href={viewerUrl} target="_blank" rel="noopener noreferrer">
          查看可视化结果（新窗口打开）
        </a>
      )}
    </div>
  );
}
