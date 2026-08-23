import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { GraphInterrupt } from "../api-types";
import { GraphInterruptPanel } from "./GraphInterruptPanel";

afterEach(cleanup);

describe("GraphInterruptPanel", () => {
  it("renders clarification details and resumes with the selected answer", async () => {
    const resume = vi.fn();
    render(<GraphInterruptPanel interrupt={clarification()} busy={false} onResume={resume} />);

    expect(screen.getByText("Treatment group")).toBeVisible();
    expect(screen.getByText("Choose one level from the uploaded metadata.")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "salt" }));
    await userEvent.click(screen.getByRole("button", { name: "Continue" }));

    expect(resume).toHaveBeenCalledWith({ kind: "clarification", interrupt_id: "interrupt-1", answer: "salt" });
  });

  it("accepts a manual clarification answer", async () => {
    const resume = vi.fn();
    render(<GraphInterruptPanel interrupt={clarification()} busy={false} onResume={resume} />);

    await userEvent.type(screen.getByLabelText("Your answer"), "use salt as the treatment");
    await userEvent.click(screen.getByRole("button", { name: "Continue" }));

    expect(resume).toHaveBeenCalledWith({ kind: "clarification", interrupt_id: "interrupt-1", answer: "use salt as the treatment" });
  });

  it("renders confirmation evidence and submits all three actions", async () => {
    const resume = vi.fn();
    render(<GraphInterruptPanel interrupt={confirmation()} busy={false} onResume={resume} />);

    expect(screen.getByText("DEG")).toBeVisible();
    expect(screen.getByText("salt")).toBeVisible();
    expect(screen.getByText("5 samples")).toBeVisible();
    expect(screen.getByText("control")).toBeVisible();
    expect(screen.getByText("4 samples")).toBeVisible();
    expect(screen.getByText("Low replicate count")).toBeVisible();

    await userEvent.click(screen.getByRole("button", { name: "Run" }));
    expect(resume).toHaveBeenLastCalledWith({ kind: "confirmation", interrupt_id: "interrupt-2", action: "run" });

    const modify = screen.getByRole("button", { name: "Modify" });
    expect(modify).toBeDisabled();
    await userEvent.type(screen.getByLabelText(/Modification/), "set cutoff to 0.01");
    await userEvent.click(modify);
    expect(resume).toHaveBeenLastCalledWith({ kind: "confirmation", interrupt_id: "interrupt-2", action: "modify", modification: "set cutoff to 0.01" });

    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(resume).toHaveBeenLastCalledWith({ kind: "confirmation", interrupt_id: "interrupt-2", action: "cancel" });
  });
});

function clarification(): GraphInterrupt {
  return {
    interrupt_id: "interrupt-1",
    payload: {
      kind: "clarification",
      question: "Which treatment should be compared?",
      missing: [{ field: "treatment_group", options: ["salt", "drought"], reason: "Choose one level from the uploaded metadata." }],
    },
  };
}

function confirmation(): GraphInterrupt {
  return {
    interrupt_id: "interrupt-2",
    payload: {
      kind: "confirmation",
      analysis_type: "DEG",
      resolved_params: { analysis_type: "DEG", contrast: { compare_field: "condition", tested_level: "salt", reference_level: "control" } },
      preview: { compare_field: "condition", tested_level: "salt", reference_level: "control", tested_count: 5, reference_count: 4 },
      warnings: [{ code: "LOW_REPLICATES", message: "Low replicate count" }],
      input_fingerprint: "sha256:input",
    },
  };
}
