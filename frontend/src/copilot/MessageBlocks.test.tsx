import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { AgentMessageResponse } from "../api-types";
import { MessageBlocks } from "./MessageBlocks";

const { mockUseJobProgressSubscription } = vi.hoisted(() => ({
  mockUseJobProgressSubscription: vi.fn(),
}));

vi.mock("../app/jobs/useJobProgressSubscription", () => ({
  useJobProgressSubscription: mockUseJobProgressSubscription,
}));

function message(blocks: AgentMessageResponse["blocks"]): AgentMessageResponse {
  return { message_id: "message-1", thread_id: "thread-1", run_id: "run-1", role: "assistant", blocks, created_at: "2026-08-03T00:00:00Z" };
}

describe("MessageBlocks", () => {
  it("renders model text as text and never as HTML", () => {
    const { container } = render(<MessageBlocks message={message([{ type: "text", text: "<img src=x onerror=alert(1)>" }])} onRetry={vi.fn()} />);
    expect(screen.getByText("<img src=x onerror=alert(1)>")).toBeVisible();
    expect(container.querySelector("img")).toBeNull();
  });

  it("shows verifiable artifact, row and checksum evidence", () => {
    render(<MessageBlocks message={message([{ type: "evidence", claims: [{ text: "Gene A is elevated.", citation: { artifact: "deg.csv", checksum: "sha256:12345678901234567890", row_ids: [4, 8] } }] }])} onRetry={vi.fn()} />);
    expect(screen.getByText("deg.csv")).toBeVisible();
    expect(screen.getByText("Rows 4, 8")).toBeVisible();
    expect(screen.getByTitle("sha256:12345678901234567890")).toBeVisible();
  });

  it("updates a queued job block from live progress and links to results", () => {
    mockUseJobProgressSubscription.mockReturnValue({
      progress: { job_id: "job-1", status: "succeeded", progress: 100 },
      error: null,
      mode: "sse",
      connectionState: "closed",
      reconnectAttempts: 0,
    });

    render(<MessageBlocks message={message([{
      type: "job",
      job_id: "job-1",
      status: "queued",
      progress: 0,
      progress_url: "/jobs/job-1",
      results_url: "/jobs/job-1/results?source=agent",
    }])} onRetry={vi.fn()} />);

    expect(screen.getByText("succeeded")).toBeVisible();
    expect(screen.getByText("100% complete")).toBeVisible();
    expect(screen.getByRole("link", { name: /Open results/ })).toHaveAttribute("href", "/jobs/job-1/results?source=agent");
  });
});
