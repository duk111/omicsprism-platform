import { ThumbsDown, ThumbsUp } from "lucide-react";
import { useEffect, useState } from "react";
import type { AgentFeedbackCategory, AgentFeedbackCreateRequest, AgentFeedbackResponse } from "../api-types";

const CATEGORIES: { value: AgentFeedbackCategory; label: string }[] = [
  { value: "incorrect_result", label: "Incorrect result" },
  { value: "missing_context", label: "Missing context" },
  { value: "bad_plan", label: "Poor plan" },
  { value: "unsafe_action", label: "Unsafe action" },
  { value: "latency", label: "Too slow" },
  { value: "other", label: "Other" },
];

export function FeedbackControls({
  feedback,
  busy,
  onSave,
}: {
  feedback?: AgentFeedbackResponse;
  busy: boolean;
  onSave: (payload: AgentFeedbackCreateRequest) => Promise<void>;
}) {
  const [expanded, setExpanded] = useState(feedback?.rating === "unhelpful");
  const [category, setCategory] = useState<AgentFeedbackCategory>(
    feedback?.failure_category ?? "incorrect_result",
  );
  const [correction, setCorrection] = useState(feedback?.correction_text ?? "");

  useEffect(() => {
    setExpanded(feedback?.rating === "unhelpful");
    setCategory(feedback?.failure_category ?? "incorrect_result");
    setCorrection(feedback?.correction_text ?? "");
  }, [feedback]);

  async function markHelpful() {
    await onSave({ rating: "helpful" });
    setExpanded(false);
  }

  return (
    <div className="message-feedback" aria-label="Response feedback">
      <div className="feedback-actions">
        <button
          type="button"
          className={feedback?.rating === "helpful" ? "selected" : ""}
          aria-label="Helpful response"
          title="Helpful"
          disabled={busy}
          onClick={() => void markHelpful()}
        ><ThumbsUp size={15} /></button>
        <button
          type="button"
          className={feedback?.rating === "unhelpful" ? "selected" : ""}
          aria-label="Unhelpful response"
          title="Not helpful"
          disabled={busy}
          onClick={() => setExpanded(value => !value || feedback?.rating !== "unhelpful")}
        ><ThumbsDown size={15} /></button>
      </div>
      {expanded && <div className="feedback-form">
        <select
          aria-label="Feedback category"
          value={category}
          disabled={busy}
          onChange={event => setCategory(event.target.value as AgentFeedbackCategory)}
        >{CATEGORIES.map(item => <option value={item.value} key={item.value}>{item.label}</option>)}</select>
        <textarea
          aria-label="Correction or detail"
          value={correction}
          maxLength={1200}
          placeholder="Optional correction"
          disabled={busy}
          onChange={event => setCorrection(event.target.value)}
        />
        <button
          type="button"
          disabled={busy}
          onClick={() => void onSave({
            rating: "unhelpful",
            failure_category: category,
            ...(correction.trim() ? { correction_text: correction.trim() } : {}),
          })}
        >{busy ? "Saving..." : "Send feedback"}</button>
      </div>}
    </div>
  );
}
