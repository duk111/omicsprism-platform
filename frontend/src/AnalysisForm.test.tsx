import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AnalysisForm } from "./App";

const { apiFetchJsonMock } = vi.hoisted(() => ({ apiFetchJsonMock: vi.fn() }));

vi.mock("./api", async importOriginal => ({
  ...await importOriginal<typeof import("./api")>(),
  apiFetchJson: apiFetchJsonMock,
}));

afterEach(cleanup);
beforeEach(() => apiFetchJsonMock.mockReset());

const passingResult = {
  analysis_type: "DEG" as const,
  ok: true,
  can_submit: true,
  files: [{ field: "counts", filename: "counts.csv", rows: 12, columns: 5 }],
  errors: [],
  warnings: [],
};

function renderForm() {
  return render(<AnalysisForm initialType="deg" onProgress={vi.fn()} onBack={vi.fn()} />);
}

async function uploadCounts(user: ReturnType<typeof userEvent.setup>) {
  const input = screen.getByText("Counts matrix (CSV) *").closest("label")?.querySelector("input");
  if (!input) throw new Error("Counts file input not found");
  await user.upload(input, new File(["id,s1\ng1,1"], "counts.csv", { type: "text/csv" }));
}

describe("AnalysisForm preflight", () => {
  it("shows file summaries and allows submission after a clean check", async () => {
    const user = userEvent.setup();
    apiFetchJsonMock.mockResolvedValue(passingResult);
    renderForm();
    await uploadCounts(user);
    await user.click(screen.getByRole("button", { name: "Check Data" }));

    expect(await screen.findByText("Data check passed. You can start the analysis.")).toBeInTheDocument();
    expect(screen.getAllByText("counts.csv").length).toBe(2);
    expect(screen.getByText(/12 rows, 5 columns/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start analysis" })).toBeEnabled();
    const body = apiFetchJsonMock.mock.calls[0][1].body as FormData;
    expect(apiFetchJsonMock.mock.calls[0][0]).toBe("/api/jobs/preflight");
    expect(body.get("counts")).toBeInstanceOf(File);
  });

  it("shows warnings while retaining a submit-ready result", async () => {
    const user = userEvent.setup();
    apiFetchJsonMock.mockResolvedValue({
      ...passingResult,
      warnings: [{ code: "sample_order_mismatch", severity: "warning", message: "Sample order differs." }],
    });
    renderForm();
    await uploadCounts(user);
    await user.click(screen.getByRole("button", { name: "Check Data" }));

    expect(await screen.findByText("Warnings")).toBeInTheDocument();
    expect(screen.getByText("Sample order differs.")).toBeInTheDocument();
    expect(screen.getByText("Data check passed. You can start the analysis.")).toBeInTheDocument();
  });

  it("shows errors and blocks any claim that the data can be submitted", async () => {
    const user = userEvent.setup();
    apiFetchJsonMock.mockResolvedValue({
      ...passingResult,
      ok: false,
      can_submit: false,
      errors: [{ code: "invalid_csv", severity: "error", field: "counts", message: "Counts file is not valid CSV." }],
    });
    renderForm();
    await uploadCounts(user);
    await user.click(screen.getByRole("button", { name: "Check Data" }));

    expect(await screen.findByText("Data check found errors. Fix them and check again before starting.")).toBeInTheDocument();
    expect(screen.getByText("counts: Counts file is not valid CSV.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Start analysis" })).toBeDisabled();
  });

  it("sends missing files to preflight and displays the returned missing-file issue", async () => {
    const user = userEvent.setup();
    apiFetchJsonMock.mockResolvedValue({
      analysis_type: "DEG", ok: false, can_submit: false, files: [],
      errors: [{ code: "missing_required_field", severity: "error", field: "counts", message: "counts is required." }],
      warnings: [],
    });
    renderForm();
    await user.click(screen.getByRole("button", { name: "Check Data" }));

    expect(await screen.findByText("counts: counts is required.")).toBeInTheDocument();
    expect(screen.getByText("No valid input files were found.")).toBeInTheDocument();
    const body = apiFetchJsonMock.mock.calls[0][1].body as FormData;
    expect(body.has("counts")).toBe(false);
    expect(screen.getByText("Data check found errors. Fix them and check again before starting.")).toBeInTheDocument();
  });

  it("treats request exceptions as a failed check and clears old results after edits", async () => {
    const user = userEvent.setup();
    apiFetchJsonMock.mockResolvedValueOnce(passingResult).mockRejectedValueOnce(new TypeError("offline"));
    renderForm();
    await uploadCounts(user);
    await user.click(screen.getByRole("button", { name: "Check Data" }));
    expect(await screen.findByText("Data check passed. You can start the analysis.")).toBeInTheDocument();

    const compareField = screen.getByPlaceholderText("e.g. group1");
    await user.type(compareField, "treatment");
    expect(screen.queryByText("Data check passed. You can start the analysis.")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Check Data" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Unable to check data. Please try again.");
    expect(screen.queryByText("Data check passed. You can start the analysis.")).not.toBeInTheDocument();
  });

  it("uses the same request fields for preflight and final submission", async () => {
    const user = userEvent.setup();
    apiFetchJsonMock.mockResolvedValueOnce(passingResult).mockResolvedValueOnce({ id: "job-1" });
    renderForm();
    await uploadCounts(user);
    const metadataInput = screen.getByText("Sample metadata table (CSV) *").closest("label")?.querySelector("input");
    if (!metadataInput) throw new Error("Metadata file input not found");
    await user.upload(metadataInput, new File(["sample_id,group1\ns1,A"], "metadata.csv", { type: "text/csv" }));
    await user.type(screen.getByPlaceholderText("e.g. group1"), "group1");
    await user.type(screen.getByPlaceholderText("e.g. Treatment_A,Treatment_B"), "A");
    await user.type(screen.getByPlaceholderText("e.g. Control"), "B");
    await user.click(screen.getByRole("button", { name: "Check Data" }));
    await screen.findByText("Data check passed. You can start the analysis.");
    const preflightBody = apiFetchJsonMock.mock.calls[0][1].body as FormData;

    await user.click(screen.getByRole("button", { name: "Start analysis" }));
    await waitFor(() => expect(apiFetchJsonMock).toHaveBeenCalledTimes(2));
    expect(apiFetchJsonMock.mock.calls[1][0]).toBe("/api/jobs");
    const submitBody = apiFetchJsonMock.mock.calls[1][1].body as FormData;
    for (const key of ["analysis_type", "counts", "metadata", "compare_field", "tested_levels", "reference_level", "padj_cutoff", "log2fc_cutoff"]) {
      expect(submitBody.get(key)).toBe(preflightBody.get(key));
    }
  });
});
