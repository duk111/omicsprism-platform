import { AlertTriangle, BookOpen, ExternalLink, FileText, FlaskConical, Lightbulb, Quote } from "lucide-react";
import { publicUrl } from "../api";
import { useJobProgressSubscription } from "../app/jobs/useJobProgressSubscription";
import type { AgentJobBlock, AgentMessageResponse } from "../api-types";

type Block = AgentMessageResponse["blocks"][number];

export function MessageBlocks({
  message,
  onRetry,
}: {
  message: AgentMessageResponse;
  onRetry: () => void;
}) {
  return <>{message.blocks.map((block, index) => (
    <BlockView key={`${message.message_id}-${index}`} block={block} onRetry={onRetry} />
  ))}</>;
}

function BlockView({ block, onRetry }: {
  block: Block;
  onRetry: () => void;
}) {
  switch (block.type) {
    case "text": return <p className="copilot-text">{block.text}</p>;
    case "advisory": {
      const generalBiology = block.category === "general_biology";
      const Icon = generalBiology ? BookOpen : Lightbulb;
      const label = generalBiology ? "Biological knowledge" : "Analysis guidance";
      return (
        <section className={`advisory-block advisory-${block.category}`} aria-label={label}>
          <h3><Icon size={16} /> {label}</h3>
          <p>{block.text}</p>
        </section>
      );
    }
    case "input_summary": return (
      <section className="message-section" aria-label="Uploaded inputs">
        <h3><FileText size={16} /> Input bundle</h3>
        <ul className="compact-list">{block.files.map(file => <li key={file.file_id}><strong>{file.field}</strong><span>{file.filename}</span><small>{formatBytes(file.size_bytes)}</small></li>)}</ul>
      </section>
    );
    case "recommendation": return (
      <section className="message-section"><h3><FlaskConical size={16} /> Recommended analyses</h3>
        {block.recommendations.map(item => <div className="recommendation-row" key={item.analysis_type}><strong>{item.display_label}</strong><p>{(item.reasons ?? []).join(" ")}</p></div>)}
      </section>
    );
    case "job": return <JobBlockView block={block} />;
    case "evidence": return (
      <section className="evidence-block"><h3><Quote size={16} /> Evidence</h3>{block.claims.length === 0 ? <p>No evidence met the requested threshold.</p> : block.claims.map((claim, index) => <article key={`${claim.citation.checksum}-${index}`}><p>{claim.text}</p><footer><code>{claim.citation.artifact}</code><span>Rows {claim.citation.row_ids.join(", ")}</span><span title={claim.citation.checksum}>{shortChecksum(claim.citation.checksum)}</span></footer></article>)}</section>
    );
    case "error": return <section className="error-block" role="alert"><AlertTriangle size={18} /><div><strong>{block.user_message}</strong>{block.request_id && <small>Request {block.request_id}</small>}</div>{block.retryable && <button type="button" className="secondary" onClick={onRetry}>Retry</button>}</section>;
    default: return null;
  }
}

function JobBlockView({ block }: { block: AgentJobBlock }) {
  const { progress } = useJobProgressSubscription(block.job_id);
  const status = progress?.status ?? block.status;
  const percent = progress?.progress ?? block.progress;
  const succeeded = status === "succeeded";
  const href = publicUrl(
    succeeded
      ? block.results_url ?? `/jobs/${encodeURIComponent(block.job_id)}/results`
      : block.progress_url,
  );
  return (
    <section className="job-block">
      <div>
        <span className={`job-status status-${status}`}>{status}</span>
        <h3>Analysis job</h3>
        <p>{Math.round(percent)}% complete</p>
      </div>
      <div className="job-progress" aria-label={`${percent}% complete`}>
        <span style={{ width: `${Math.max(0, Math.min(100, percent))}%` }} />
      </div>
      <a href={href}>
        {succeeded ? "Open results" : "Track job"}
        <ExternalLink size={15} />
      </a>
    </section>
  );
}

const formatBytes = (bytes: number) => bytes < 1024 * 1024 ? `${Math.ceil(bytes / 1024)} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`;
const shortChecksum = (value: string) => value.length > 20 ? `${value.slice(0, 16)}...` : value;
