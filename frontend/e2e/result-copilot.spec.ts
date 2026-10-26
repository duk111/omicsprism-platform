import { expect, test } from "@playwright/test";

test.beforeEach(async ({ context }) => {
  await context.addCookies([{ name: "omicsprism_session", value: "pr10-browser-user", domain: "127.0.0.1", path: "/" }]);
});

for (const analysis of ["deg", "dem", "gma"]) {
  test(`${analysis} opens in place with visible scope and no automatic turn`, async ({ page }, info) => {
    let turns = 0;
    page.on("request", request => { if (request.method() === "POST" && request.url().endsWith("/turns")) turns++; });
    await page.goto(`/jobs/pr10-${analysis}/results`);
    await page.getByRole("button", { name: "询问 Copilot", exact: true }).click();
    const panel = page.getByRole("complementary", { name: "结果 Copilot" });
    await expect(panel.getByText(`当前分析：${analysis.toUpperCase()}`)).toBeVisible();
    await expect(panel.getByText(`当前任务：pr10-${analysis}`)).toBeVisible();
    await expect(panel.getByLabel("Message Copilot")).toBeVisible();
    await panel.locator(".context-suggestions button").first().click();
    await expect(panel.getByLabel("Send message")).toBeEnabled();
    expect(turns).toBe(0);
    const before = page.url();
    await panel.getByRole("button", { name: "关闭 Copilot" }).click();
    await expect(panel).toBeHidden();
    await page.getByRole("button", { name: "询问 Copilot", exact: true }).click();
    await expect(panel.getByLabel("Message Copilot")).not.toHaveValue("");
    expect(page.url()).toBe(before);
    await page.screenshot({ path: info.outputPath(`${analysis}-panel.png`), fullPage: true });
  });
}

test("DEG question queries actual disk Artifact and returns verified minimum-padj evidence over SSE", async ({ page }, info) => {
  await page.goto("/jobs/pr10-deg/results");
  await page.getByRole("button", { name: "询问 Copilot", exact: true }).click();
  const panel = page.getByRole("complementary", { name: "结果 Copilot" });
  await panel.getByLabel("Message Copilot").fill("当前任务中 padj 最小的基因是什么？");
  await expect(panel.getByLabel("Send message")).toBeEnabled();
  const response = page.waitForResponse(res => res.request().method() === "POST" && res.url().endsWith("/turns"));
  await panel.getByLabel("Send message").click();
  const accepted = await response;
  expect(accepted.status()).toBe(202);
  const input = accepted.request().postDataJSON();
  expect(input.focus_job_ids).toEqual(["pr10-deg"]);
  expect(input.result_view).toMatchObject({ job_id: "pr10-deg", analysis_type: "DEG", result_type: "overview" });
  await expect(panel.locator(".evidence-block")).toContainText("gene_up has padj 4.335429232883268e-101", { timeout: 20000 });
  await expect(panel.locator(".evidence-block")).toContainText("outputs/deg.all.csv");
  await expect(panel.locator(".evidence-block")).toContainText("Rows 1");
  expect(page.url()).toContain("/jobs/pr10-deg/results");
  const evidence = await (await page.request.get("http://127.0.0.1:8011/__test__/evidence")).json();
  const observed = evidence.observations.find((item: any) => item.thread_id === (awaitedThreadId(accepted.url())));
  expect(observed.tool).toBe("query_artifact");
  expect(observed.arguments).toMatchObject({ job_id: "pr10-deg", artifact: "outputs/deg.all.csv", sort: "padj asc", limit: 1 });
  expect(observed.result.rows[0].gene_id).toBe("gene_up");
  await page.screenshot({ path: info.outputPath("deg-verified-answer.png"), fullPage: true });
  await panel.getByLabel("关闭 Copilot").click();
  // Same Job: change to figure, then table without clearing the answer.
  await page.getByLabel("当前结果", { exact: true }).selectOption("outputs/volcano.svg");
  await page.locator(".fv-toolbar-right").getByRole("button", { name: "询问 Copilot" }).click();
  await expect(panel.getByText("当前结果：Volcano", { exact: true })).toBeVisible();
  await expect(panel.locator(".evidence-block")).toContainText("gene_up");
  // Typing a scientific value must not activate the viewer's global zoom shortcuts.
  const zoom = await page.locator(".fv-toolbar-center").textContent();
  await panel.getByLabel("Message Copilot").pressSequentially("padj=0.05 log2FoldChange=-1");
  expect(await page.locator(".fv-toolbar-center").textContent()).toBe(zoom);
  await panel.getByLabel("Message Copilot").fill("");
  await page.screenshot({ path: info.outputPath("deg-figure-and-copilot.png"), fullPage: true });
  // Dismiss viewer before choosing a table (small screens use an overlay).
  await panel.getByLabel("关闭 Copilot").click();
  await page.locator(".fv-close-btn").click();
  await page.getByLabel("当前结果", { exact: true }).selectOption("outputs/deg.all.csv");
  await page.getByRole("button", { name: "询问 Copilot", exact: true }).click();
  await expect(panel.getByText("当前结果：deg.all.csv")).toBeVisible();
  await expect(panel.locator(".evidence-block")).toContainText("gene_up");
  await panel.getByLabel("Message Copilot").fill("当前结果中 padj 最小的基因是什么？");
  const next = page.waitForRequest(request => request.method() === "POST" && request.url().endsWith("/turns"));
  await expect(panel.getByLabel("Send message")).toBeEnabled();
  await panel.getByLabel("Send message").click();
  expect((await next).postDataJSON().result_view).toMatchObject({ result_type: "table", artifact: "outputs/deg.all.csv" });
  await expect(panel.locator(".evidence-block")).toHaveCount(2);
  // Reload has an explicit fresh-session policy, with history still on the full page.
  await page.reload();
  await expect(page.getByRole("complementary", { name: "结果 Copilot" })).toHaveCount(0);
  await page.getByRole("button", { name: "询问 Copilot", exact: true }).click();
  await expect(panel.locator(".evidence-block")).toHaveCount(0);
});

function awaitedThreadId(url: string) { return url.split("/threads/")[1].split("/")[0]; }

test("backend refuses foreign, deleted and forged references", async ({ page }) => {
  const root = "http://127.0.0.1:8011/api/agent/threads";
  const thread = await (await page.request.post(root, { data: { focus_job_ids: ["pr10-deg"] } })).json();
  const invalid = await page.request.post(`${root}/${thread.thread_id}/turns`, { headers: { "Idempotency-Key": "bad-view" }, data: {
    message: "查询当前结果", focus_job_ids: ["pr10-deg"], result_view: { job_id: "pr10-deg", analysis_type: "DEG", result_type: "table", artifact: "../../secret.csv" },
  } });
  expect(invalid.status()).toBe(404);
  const foreign = await page.request.post(root, { headers: { Cookie: "omicsprism_session=other-user" }, data: { focus_job_ids: ["pr10-deg"] } });
  expect(foreign.status()).toBe(404);
  expect((await page.request.post(root, { data: { focus_job_ids: ["missing-job"] } })).status()).toBe(404);
  expect((await page.request.post(root, { data: { focus_job_ids: ["pr10-deleted"] } })).status()).toBe(404);
});
