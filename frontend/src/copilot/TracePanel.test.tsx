import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { TracePanel } from "./TracePanel";

describe("TracePanel", () => {
  it("renders safe trace summaries and refreshes on demand", () => {
    const onRefresh = vi.fn();
    render(<TracePanel
      loading={false}
      onRefresh={onRefresh}
      events={[{
        event_id: "event-1",
        trace_id: "trace-1",
        thread_id: "thread-1",
        turn_id: "turn-1",
        run_id: "run-1",
        event_type: "model.call",
        component: "model",
        name: "chat.completions",
        schema_version: "main-output.v1",
        graph_version: "agent-graph.v3",
        model_provider: "recorded-fixture",
        model_name: "fixture",
        outcome: "ok",
        latency_ms: 4.4,
        prompt_tokens: 2,
        completion_tokens: 3,
        total_tokens: 5,
        usage_status: "reported",
        retry_count: 0,
        created_at: new Date().toISOString(),
      }]}
    />);

    expect(screen.getByText("1 event")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Refresh trace" }));
    expect(onRefresh).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole("button", { name: /Trace evidence/ }));
    expect(screen.getByText("chat.completions")).toBeInTheDocument();
    expect(screen.queryByText("trace-1")).not.toBeInTheDocument();
  });
});
