import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { AgentMessageResponse } from "../api-types";
import { MessageBlocks } from "./MessageBlocks";

function message(blocks: AgentMessageResponse["blocks"]): AgentMessageResponse {
  return { message_id: "message-1", thread_id: "thread-1", run_id: "run-1", role: "assistant", blocks, created_at: "2026-08-03T00:00:00Z" };
}

describe("MessageBlocks", () => {
  it("renders model text as text and never as HTML", () => {
    const { container } = render(<MessageBlocks message={message([{ type: "text", text: "<img src=x onerror=alert(1)>" }])} approvalBusy={null} onApproval={vi.fn()} onRetry={vi.fn()} />);
    expect(screen.getByText("<img src=x onerror=alert(1)>")).toBeVisible();
    expect(container.querySelector("img")).toBeNull();
  });

  it("renders bounded biological advice as labeled plain text", () => {
    const { container } = render(<MessageBlocks message={message([{
      type: "advisory",
      category: "general_biology",
      text: "ABA supports drought responses. <script>alert(1)</script>",
    }])} approvalBusy={null} onApproval={vi.fn()} onRetry={vi.fn()} />);

    expect(screen.getByRole("region", { name: "Biological knowledge" })).toBeVisible();
    expect(screen.getByText("ABA supports drought responses. <script>alert(1)</script>")).toBeVisible();
    expect(container.querySelector("script")).toBeNull();
  });

  it("distinguishes analysis guidance from biological knowledge", () => {
    render(<MessageBlocks message={message([{
      type: "advisory",
      category: "analysis_guidance",
      text: "Upload counts and metadata before execution.",
    }])} approvalBusy={null} onApproval={vi.fn()} onRetry={vi.fn()} />);

    expect(screen.getByRole("region", { name: "Analysis guidance" })).toBeVisible();
  });

  it("submits an explicit approval decision with the bound hash", async () => {
    const approve = vi.fn();
    render(<MessageBlocks message={message([{ type: "approval", approval_id: "approval-1", plan_hash: "sha256:bound", status: "pending", expires_at: "2026-08-03T01:00:00Z" }])} approvalBusy={null} onApproval={approve} onRetry={vi.fn()} />);
    await userEvent.click(screen.getByRole("button", { name: "Approve plan" }));
    expect(approve).toHaveBeenCalledWith("approval-1", "approve", "sha256:bound");
  });

  it("shows verifiable artifact, row and checksum evidence", () => {
    render(<MessageBlocks message={message([{ type: "evidence", claims: [{ text: "Gene A is elevated.", citation: { artifact: "deg.csv", checksum: "sha256:12345678901234567890", row_ids: [4, 8] } }] }])} approvalBusy={null} onApproval={vi.fn()} onRetry={vi.fn()} />);
    expect(screen.getByText("deg.csv")).toBeVisible();
    expect(screen.getByText("Rows 4, 8")).toBeVisible();
    expect(screen.getByTitle("sha256:12345678901234567890")).toBeVisible();
  });
});
