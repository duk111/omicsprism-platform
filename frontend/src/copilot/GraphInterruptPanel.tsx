import { Check, Pencil, Play, X } from "lucide-react";
import { useEffect, useState } from "react";
import type { ConfirmationPayload, GraphInterrupt } from "../api-types";
import type { GraphResumeRequest } from "./agentApi";

export function GraphInterruptPanel({ interrupt, busy, onResume }: {
  interrupt: GraphInterrupt;
  busy: boolean;
  onResume: (request: GraphResumeRequest) => void;
}) {
  const [answer, setAnswer] = useState("");
  const payload = interrupt.payload;

  useEffect(() => setAnswer(""), [interrupt.interrupt_id]);

  if (isConfirmationPayload(payload)) {
    return (
      <ConfirmationPanel
        payload={payload}
        interruptId={interrupt.interrupt_id}
        busy={busy}
        modification={answer}
        onModification={setAnswer}
        onResume={onResume}
      />
    );
  }

  return (
    <section className="graph-interrupt clarification-panel" aria-label="Analysis clarification">
      <div className="interrupt-heading"><span>Clarification</span><h3>{payload.question}</h3></div>
      {(payload.missing ?? []).length > 0 && (
        <ul className="clarification-list">
          {(payload.missing ?? []).map(item => (
            <li key={item.field}>
              <strong>{humanize(item.field)}</strong>
              <p>{item.reason}</p>
              {(item.options ?? []).length > 0 && (
                <div className="option-list">
                  {(item.options ?? []).map(option => (
                    <button type="button" className={answer === option ? "selected" : ""} key={option} onClick={() => setAnswer(option)}>{option}</button>
                  ))}
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
      <div className="interrupt-response">
        <label htmlFor={`clarification-${interrupt.interrupt_id}`}>Your answer</label>
        <textarea id={`clarification-${interrupt.interrupt_id}`} rows={2} value={answer} onChange={event => setAnswer(event.target.value)} />
        <button type="button" className="primary" disabled={busy || !answer.trim()} onClick={() => onResume({ kind: "clarification", interrupt_id: interrupt.interrupt_id, answer: answer.trim() })}><Check size={16} />Continue</button>
      </div>
    </section>
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
        </div>
      )}
      {(payload.warnings ?? []).map(warning => <p className="inline-warning" key={`${warning.code}-${warning.field ?? ""}`}>{warning.message}</p>)}
      <div className="modification-row">
        <label htmlFor={`modification-${interruptId}`}><Pencil size={14} />Modification</label>
        <input id={`modification-${interruptId}`} value={modification} onChange={event => onModification(event.target.value)} />
      </div>
      <div className="interrupt-actions">
        <button type="button" className="secondary danger-action" disabled={busy} onClick={() => onResume({ kind: "confirmation", interrupt_id: interruptId, action: "cancel" })}><X size={16} />Cancel</button>
        <button type="button" className="secondary" disabled={busy || !modification.trim()} onClick={() => onResume({ kind: "confirmation", interrupt_id: interruptId, action: "modify", modification: modification.trim() })}><Pencil size={16} />Modify</button>
        <button type="button" className="primary" disabled={busy} onClick={() => onResume({ kind: "confirmation", interrupt_id: interruptId, action: "run" })}><Play size={16} />Run</button>
      </div>
    </section>
  );
}

const humanize = (value: string) => value.replace(/_/g, " ").replace(/^./, char => char.toUpperCase());
const isConfirmationPayload = (payload: GraphInterrupt["payload"]): payload is ConfirmationPayload => "analysis_type" in payload;
