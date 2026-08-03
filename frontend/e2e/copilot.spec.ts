import { expect, test, type Page, type Route } from "@playwright/test";

const now = "2026-08-03T08:00:00Z";
const thread = { thread_id: "thread-1", title: "Salt response analysis", current_run_id: "run-1", status: "active", version: 1, created_at: now, updated_at: now };
const run = { run_id: "run-1", thread_id: "thread-1", active_profile: "analysis", state: "WAIT_EXECUTION_CONFIRMATION", step_no: 3, plan_id: "plan-1", plan_hash: "sha256:plan", pending_approval_id: "approval-1", focus: { in_scope_job_ids: [], resolved_entities: {}, last_citation: null }, model_calls: 1, tool_calls: 2, status: "suspended", version: 2 };
const planMessages = [
  { message_id: "message-1", thread_id: "thread-1", run_id: "run-1", role: "user", blocks: [{ type: "text", text: "Find differential genes in salt-treated samples." }], created_at: now },
  { message_id: "message-2", thread_id: "thread-1", run_id: "run-1", role: "assistant", blocks: [
    { type: "plan", plan_id: "plan-1", plan_hash: "sha256:plan", analysis_type: "differential", requested_params: {}, effective_params: { padj_cutoff: 0.05, log2fc_cutoff: 1 }, contrasts: [{ tested: "salt", reference: "control" }], warnings: [], expires_at: "2026-08-03T09:00:00Z" },
    { type: "approval", approval_id: "approval-1", plan_hash: "sha256:plan", status: "pending", expires_at: "2026-08-03T09:00:00Z" },
  ], created_at: now },
];

async function mockCopilot(page: Page, mode: "plan" | "evidence" | "offline") {
  let approved = false;
  await page.route("**/api/agent/threads/**/stream", route => route.fulfill({ status: 200, contentType: "text/event-stream", body: "" }));
  await page.route("**/api/agent/**", async route => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    if (request.method() === "POST" && path === "/api/agent/threads") return json(route, 201, thread);
    if (request.method() === "POST" && path.endsWith("/input-bundles")) return json(route, 201, { bundle_id: "bundle-1", thread_id: "thread-1", status: "active", expires_at: "2026-08-03T09:00:00Z", created_at: now, files: [{ file_id: "file-1", field: "counts", filename: "counts.csv", checksum: "sha256:file", content_type: "text/csv", size_bytes: 12, created_at: now }] });
    if (request.method() === "POST" && path.endsWith("/turns")) return json(route, 202, { turn_id: "turn-1", thread_id: "thread-1", run_id: "run-1", status: "completed", attempt: 1, error_code: null, created_at: now, updated_at: now, started_at: now, completed_at: now });
    if (request.method() === "POST" && path.includes("/approvals/")) { approved = true; return json(route, 202, { turn_id: "turn-approved", thread_id: "thread-1", run_id: "run-1", status: "queued", attempt: 0, error_code: null, created_at: now, updated_at: now, started_at: null, completed_at: null }); }
    if (path.endsWith("/messages")) {
      const evidence = [{ message_id: "evidence-1", thread_id: "thread-1", run_id: "run-1", role: "assistant", blocks: [{ type: "evidence", claims: [{ text: "T02 contains 14 high-confidence associations.", citation: { artifact: "T02_High_Confidence_Network.csv", checksum: "sha256:a2a84e1c786525426799f0c6593075f", row_ids: [2, 5, 9] } }] }], created_at: now }];
      const error = [{ message_id: "error-1", thread_id: "thread-1", run_id: "run-1", role: "assistant", blocks: [{ type: "error", code: "model_unavailable", user_message: "Copilot model is temporarily unavailable.", retryable: true, request_id: "request-1" }], created_at: now }];
      const job = [{ message_id: "job-1", thread_id: "thread-1", run_id: "run-1", role: "assistant", blocks: [{ type: "job", job_id: "job-1", status: "queued", progress: 0, progress_url: "/jobs/job-1", results_url: null }], created_at: now }];
      return json(route, 200, { messages: mode === "evidence" ? evidence : mode === "offline" ? error : approved ? [...planMessages, ...job] : planMessages, next_cursor: null });
    }
    if (path.endsWith("/turns")) return json(route, 200, { turns: [], next_cursor: null });
    if (path === "/api/agent/threads/thread-1") return json(route, 200, { thread, run: mode === "evidence" ? { ...run, active_profile: "interpretation", state: "AWAIT_FOLLOWUP", focus: { ...run.focus, in_scope_job_ids: ["job-gma"] } } : run });
    if (path === "/api/agent/threads") return json(route, 200, { threads: [thread], next_cursor: null });
    return route.fallback();
  });
}

test("analyze plan requires explicit approval and yields a job card", async ({ page }, testInfo) => {
  await mockCopilot(page, "plan");
  await page.goto("/copilot");
  await expect(page.getByRole("heading", { name: "Analysis plan" })).toBeVisible();
  await page.locator('input[type="file"]').setInputFiles({ name: "counts.csv", mimeType: "text/csv", buffer: Buffer.from("gene,s1\nA,1\n") });
  await expect(page.getByText("counts.csv")).toBeVisible();
  const queued = page.waitForRequest(request => request.method() === "POST" && request.url().endsWith("/turns"));
  await page.getByLabel("Message Copilot").fill("Use the attached counts.");
  await page.getByLabel("Message Copilot").press("Enter");
  await queued;
  await expect(page.getByText("counts.csv")).toHaveCount(0);
  await page.getByRole("button", { name: "Approve plan" }).click();
  await expect(page.getByRole("heading", { name: "Analysis job" })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("analyze-approve-job.png"), fullPage: true });
});

test("focused result renders verifiable citations", async ({ page }, testInfo) => {
  await mockCopilot(page, "evidence");
  await page.goto("/copilot?job_id=job-gma");
  await expect(page.getByText("T02_High_Confidence_Network.csv")).toBeVisible();
  await expect(page.getByText("Rows 2, 5, 9")).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("interpret-citation.png"), fullPage: true });
});

test("model outage is explicit while the original workbench remains reachable", async ({ page }, testInfo) => {
  await mockCopilot(page, "offline");
  await page.goto("/copilot");
  await expect(page.getByText("Copilot model is temporarily unavailable.")).toBeVisible();
  await page.getByRole("button", { name: "Home" }).click();
  await expect(page.getByRole("heading", { name: "OmicsPrism" })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("model-off-workbench.png"), fullPage: true });
});

function json(route: Route, status: number, body: unknown) { return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) }); }
