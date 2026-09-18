import { Pencil, Play, X } from "lucide-react";
import { useEffect, useState } from "react";
import type { ConfirmationPayload, GraphInterrupt } from "../api-types";
import type { GraphResumeRequest } from "./agentApi";

export function GraphInterruptPanel({ interrupt, busy, onResume }: {
  interrupt: GraphInterrupt;
  busy: boolean;
  onResume: (request: GraphResumeRequest) => void;
}) {
  const [answer, setAnswer] = useState("");
  useEffect(() => setAnswer(""), [interrupt.interrupt_id]);
  return (
    <ConfirmationPanel
      payload={interrupt.payload}
      interruptId={interrupt.interrupt_id}
      busy={busy}
      modification={answer}
      onModification={setAnswer}
      onResume={onResume}
    />
  );
}

function ConfirmationPanel({ payload, interruptId, busy, modification, onModification, onResume }: {
  payload: ConfirmationPayload;
  interruptId: string;
  busy: boolean;
  modification: string;
  onModification: (value: string) => void;
  onResume: (request: GraphResumeRequest) => void;
}) {
  const preview = payload.preview;
  return (
    <section className="graph-interrupt confirmation-panel" aria-label="Analysis confirmation">
      <div className="interrupt-heading"><span>{payload.analysis_type}</span><h3>Confirm analysis</h3></div>
      {preview && (
        <div className="confirmation-preview">
          <div><span>Comparison field</span><strong>{preview.compare_field}</strong></div>
          <div><span>Experimental group</span><strong>{preview.tested_level}</strong><small>{preview.tested_count} samples</small></div>
          <div><span>Reference group</span><strong>{preview.reference_level}</strong><small>{preview.reference_count} samples</small></div>
          {preview.scope && <div><span>Sample scope</span><strong>{preview.scope.mode}</strong><small>{scopeDetails(preview.scope)}</small></div>}
          {(preview.same_fields ?? []).length > 0 && <div><span>Stratified by</span><strong>{preview.same_fields!.join(", ")}</strong><small>{formatSameValues(preview.same_values)}</small></div>}
        </div>
      )}
      {(payload.warnings ?? []).map(warning => <p className="inline-warning" key={`${warning.code}-${warning.field ?? ""}`}>{warning.message}</p>)}
      <div className="modification-row">
        <label htmlFor={`modification-${interruptId}`}><Pencil size={14} />Modification</label>
        <input id={`modification-${interruptId}`} value={modification} onChange={event => onModification(event.target.value)} />
      </div>
      <div className="interrupt-actions">
        <button type="button" className="secondary danger-action" disabled={busy} onClick={() => onResume({ kind: "confirmation", interrupt_id: interruptId, plan_id: payload.plan_id, plan_version: payload.plan_version, approve: false })}><X size={16} />Cancel</button>
        <button type="button" className="secondary" disabled={busy || !modification.trim()} onClick={() => onResume({ kind: "confirmation", interrupt_id: interruptId, plan_id: payload.plan_id, plan_version: payload.plan_version, message: modification.trim() })}><Pencil size={16} />Modify</button>
        <button type="button" className="primary" disabled={busy} onClick={() => onResume({ kind: "confirmation", interrupt_id: interruptId, plan_id: payload.plan_id, plan_version: payload.plan_version, approve: true })}><Play size={16} />Run</button>
      </div>
    </section>
  );
}

const scopeDetails = (scope: NonNullable<ConfirmationPayload["preview"]>["scope"]) => {
  if (!scope) return "";
  const fixed = Object.entries(scope.fixed_filters ?? {}).map(([field, value]) => `${field}=${value}`);
  const blocking = scope.blocking_fields ?? [];
  return [...fixed, blocking.length ? `by ${blocking.join(", ")}` : ""].filter(Boolean).join("; ") || "All samples";
};
const formatSameValues = (values: Record<string, string> | undefined) =>
  Object.entries(values ?? {}).map(([field, value]) => `${field}=${value}`).join(", ");
