import { AlertTriangle, BookOpen, Check, ExternalLink, FileText, FlaskConical, Lightbulb, Quote, X } from "lucide-react";
import type { AgentMessageResponse, AgentApprovalDecision } from "../api-types";

type Block = AgentMessageResponse["blocks"][number];

export function MessageBlocks({
  message,
  approvalBusy,
  onApproval,
  onRetry,
}: {
  message: AgentMessageResponse;
  approvalBusy: string | null;
  onApproval: (approvalId: string, decision: AgentApprovalDecision, planHash: string) => void;
  onRetry: () => void;
}) {
  return <>{message.blocks.map((block, index) => (
    <BlockView key={`${message.message_id}-${index}`} block={block} approvalBusy={approvalBusy} onApproval={onApproval} onRetry={onRetry} />
  ))}</>;
}

function BlockView({ block, approvalBusy, onApproval, onRetry }: {
  block: Block;
  approvalBusy: string | null;
  onApproval: (approvalId: string, decision: AgentApprovalDecision, planHash: string) => void;
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
    case "plan": {
      const params = Object.entries(block.effective_params).filter(([key]) => !CONTRAST_PARAM_KEYS.has(key));
      return (
        <section className="message-section plan-block"><div className="block-heading"><div><span className="analysis-label">{analysisLabel(block.analysis_type)}</span><h3>Analysis plan</h3></div><time>{formatDate(block.expires_at)}</time></div>
          {block.contrasts.length > 0 && <div className="contrast-list"><strong>Comparisons</strong>{block.contrasts.map((contrast, index) => <ContrastView key={index} contrast={contrast} />)}</div>}
          {params.length > 0 && <><h4>Analysis settings</h4><dl>{params.map(([key, value]) => <div key={key}><dt>{paramLabel(key)}</dt><dd>{formatParam(value)}</dd></div>)}</dl></>}
          {(block.warnings ?? []).map(item => <p className="inline-warning" key={item}><AlertTriangle size={15} />{item}</p>)}
        </section>
      );
    }
    case "approval": {
      const pending = block.status === "pending";
      return <section className="approval-panel" aria-label="Plan approval"><div><h3>Review required</h3><p>{pending ? "Approve this exact plan to continue." : `This approval is ${block.status}.`}</p></div>
        {pending && <div className="approval-actions"><button type="button" className="secondary danger-action" disabled={approvalBusy === block.approval_id} onClick={() => onApproval(block.approval_id, "reject", block.plan_hash)}><X size={16} />Reject</button><button type="button" className="primary" disabled={approvalBusy === block.approval_id} onClick={() => onApproval(block.approval_id, "approve", block.plan_hash)}><Check size={16} />Approve plan</button></div>}
      </section>;
    }
    case "job": return (
      <section className="job-block"><div><span className={`job-status status-${block.status}`}>{block.status}</span><h3>Analysis job</h3><p>{Math.round(block.progress)}% complete</p></div><div className="job-progress" aria-label={`${block.progress}% complete`}><span style={{ width: `${Math.max(0, Math.min(100, block.progress))}%` }} /></div><a href={block.results_url || block.progress_url}>{block.results_url ? "Open results" : "Track job"}<ExternalLink size={15} /></a></section>
    );
    case "evidence": return (
      <section className="evidence-block"><h3><Quote size={16} /> Evidence</h3>{block.claims.length === 0 ? <p>No evidence met the requested threshold.</p> : block.claims.map((claim, index) => <article key={`${claim.citation.checksum}-${index}`}><p>{claim.text}</p><footer><code>{claim.citation.artifact}</code><span>Rows {claim.citation.row_ids.join(", ")}</span><span title={claim.citation.checksum}>{shortChecksum(claim.citation.checksum)}</span></footer></article>)}</section>
    );
    case "error": return <section className="error-block" role="alert"><AlertTriangle size={18} /><div><strong>{block.user_message}</strong>{block.request_id && <small>Request {block.request_id}</small>}</div>{block.retryable && <button type="button" className="secondary" onClick={onRetry}>Retry</button>}</section>;
    default: return null;
  }
}

function ContrastView({ contrast }: { contrast: Record<string, unknown> }) {
  const tested = String(contrast.tested_level ?? "-");
  const reference = String(contrast.reference_level ?? "-");
  const testedCount = Number(contrast.tested_count ?? 0);
  const referenceCount = Number(contrast.reference_count ?? 0);
  const sameValues = isRecord(contrast.same_values) ? Object.entries(contrast.same_values) : [];
  return <div className="contrast-item">
    <div><span>Comparison field</span><strong>{String(contrast.compare_field ?? "-")}</strong></div>
    <div><span>Experimental group</span><strong>{tested}</strong><small>{testedCount} samples</small></div>
    <div><span>Reference group</span><strong>{reference}</strong><small>{referenceCount} samples</small></div>
    {sameValues.length > 0 && <div><span>Matched strata</span><strong>{sameValues.map(([key, value]) => `${humanize(key)}: ${String(value)}`).join(", ")}</strong></div>}
  </div>;
}

const formatBytes = (bytes: number) => bytes < 1024 * 1024 ? `${Math.ceil(bytes / 1024)} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`;
const formatDate = (value: string) => new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
const humanize = (value: string) => value.replace(/_/g, " ").replace(/^./, (char: string) => char.toUpperCase());
const analysisLabel = (value: string) => ({ differential: "DEG", dem: "DEM", correlation: "GMA" }[value] || value);
const shortChecksum = (value: string) => value.length > 20 ? `${value.slice(0, 16)}...` : value;
const CONTRAST_PARAM_KEYS = new Set(["compare_field", "tested_levels", "reference_level", "same_fields"]);
const PARAM_LABELS: Record<string, string> = {
  padj_cutoff: "Adjusted P-value cutoff",
  log2fc_cutoff: "Absolute log2 fold-change cutoff",
  min_total_count: "Minimum total count",
  min_replicates: "Minimum replicates per group",
  normalize: "Normalization",
  filter_low_expression: "Low-expression filtering",
};
const paramLabel = (key: string) => PARAM_LABELS[key] || humanize(key);
const formatParam = (value: unknown) => typeof value === "boolean" ? (value ? "Enabled" : "Disabled") : String(value ?? "-");
const isRecord = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);
