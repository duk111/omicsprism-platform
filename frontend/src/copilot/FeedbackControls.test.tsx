import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { FeedbackControls } from "./FeedbackControls";

afterEach(cleanup);

describe("FeedbackControls", () => {
  it("submits structured negative feedback only after a category is selected", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn().mockResolvedValue(undefined);
    render(<FeedbackControls busy={false} onSave={onSave} />);

    await user.click(screen.getByRole("button", { name: "Unhelpful response" }));
    await user.selectOptions(screen.getByLabelText("Feedback category"), "missing_context");
    await user.type(screen.getByLabelText("Correction or detail"), "Include batch information.");
    await user.click(screen.getByRole("button", { name: "Send feedback" }));

    expect(onSave).toHaveBeenCalledWith({
      rating: "unhelpful", failure_category: "missing_context",
      correction_text: "Include batch information.",
    });
  });

  it("submits helpful feedback without hidden identifiers", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn().mockResolvedValue(undefined);
    render(<FeedbackControls busy={false} onSave={onSave} />);
    await user.click(screen.getByRole("button", { name: "Helpful response" }));
    expect(onSave).toHaveBeenCalledWith({ rating: "helpful" });
  });
});
