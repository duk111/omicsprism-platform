import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import App from "../App";
import CopilotPage, { type ResultCopilotContext } from "./CopilotPage";

const { fetchJson } = vi.hoisted(() => ({ fetchJson: vi.fn() }));
vi.mock("../api", async original => ({ ...await original<typeof import("../api")>(), apiFetchJson: fetchJson }));

class FakeEventSource extends EventTarget {
  static instances: FakeEventSource[] = [];
  onerror: ((event: Event) => void) | null = null;
  onopen: (() => void) | null = null;
  close = vi.fn();
  constructor(public url: string) { super(); FakeEventSource.instances.push(this); }
}

const now = "2026-10-26T00:00:00Z";
let created = 0;
const scopes = new Map<string, string[]>();
const messages = new Map<string, unknown[]>();
const context: ResultCopilotContext = { jobId: "job-a", analysisType: "deg", resultType: "overview", title: "结果总览" };

function thread(id: string) { return { thread_id: id, title: id, created_at: now, updated_at: now }; }

beforeEach(() => {
  created = 0; scopes.clear(); messages.clear(); FakeEventSource.instances = [];
  vi.stubGlobal("EventSource", FakeEventSource);
  Element.prototype.scrollIntoView = vi.fn();
  fetchJson.mockReset().mockImplementation(async (url: string, init?: RequestInit) => {
    const body = init?.body ? JSON.parse(init.body as string) : null;
    if (url === "/api/agent/threads") {
      if (init?.method === "POST") {
        const id = `thread-${++created}`; scopes.set(id, body.focus_job_ids); return thread(id);
      }
      return { threads: [] };
    }
    if (url.startsWith("/api/jobs/")) {
      const id = url.split("/")[3];
      if (url.endsWith("/files")) return { result_files: [{ path: "outputs/salt.all.csv", name: "salt.all.csv", size_bytes: 30, download_url: "/table" }], report_links: {} };
      if (url.endsWith("/images")) return [];
      return { id, analysis_type: id.split("-")[1], status: "succeeded" };
    }
    const id = url.split("/")[4];
    if (url.endsWith("/turns") && init?.method === "POST") {
      messages.set(id, [{ message_id: "message-1", role: "user", blocks: [{ type: "text", text: body.message }] }]);
      return { turn_id: "turn-1", status: "completed" };
    }
    if (url.endsWith("/messages")) return { messages: messages.get(id) ?? [] };
    if (url.endsWith("/turns")) return { turns: [] };
    if (url.endsWith("/pending-interrupt")) return null;
    if (url.endsWith("/job-waits")) return { waits: [] };
    if (url.includes("/trace")) return { events: [] };
    return { thread: thread(id), run: { focus: { in_scope_job_ids: scopes.get(id) ?? [] }, version: 1 } };
  });
});
afterEach(() => { cleanup(); vi.useRealTimers(); vi.unstubAllGlobals(); });

it.each(["deg", "dem", "gma"])("opens %s results in place and sends the current job and table", async analysis => {
  window.history.replaceState({}, "", `/jobs/job-${analysis}/results`);
  render(<App />);
  const open = await screen.findByRole("button", { name: "询问 Copilot" });
  await waitFor(() => expect(open).toBeEnabled());
  fireEvent.click(open);
  const panel = await screen.findByRole("complementary", { name: "结果 Copilot" });
  await waitFor(() => expect(created).toBe(1));
  expect(within(panel).getByText(`当前分析：${analysis.toUpperCase()}`)).toBeVisible();
  expect(within(panel).getByText(`当前任务：job-${analysis}`)).toBeVisible();
  fireEvent.change(screen.getByRole("combobox", { name: "当前结果" }), { target: { value: "outputs/salt.all.csv" } });
  expect(within(panel).getByText("当前结果：salt.all.csv")).toBeVisible();
  // Opening and choosing a suggestion must not invoke a model turn.
  fireEvent.click(within(panel).getAllByRole("button").find(button => button.closest(".context-suggestions"))!);
  expect(fetchJson.mock.calls.filter(([url, init]) => url.endsWith("/turns") && init?.method === "POST")).toHaveLength(0);
  const send = within(panel).getByRole("button", { name: "Send message" });
  await waitFor(() => expect(send).toBeEnabled());
  fireEvent.click(send);
  await waitFor(() => expect(fetchJson.mock.calls.some(([url, init]) => url.endsWith("/turns") && init?.method === "POST")).toBe(true));
  const [, init] = fetchJson.mock.calls.find(([url, init]) => url.endsWith("/turns") && init?.method === "POST")!;
  expect(JSON.parse(init.body)).toMatchObject({ focus_job_ids: [`job-${analysis}`], result_view: {
    job_id: `job-${analysis}`, analysis_type: analysis.toUpperCase(), result_type: "table", artifact: "outputs/salt.all.csv",
  } });
  expect(window.location.pathname).toBe(`/jobs/job-${analysis}/results`);
  await waitFor(() => expect(within(panel).getByText("You")).toBeVisible());
  fireEvent.click(within(panel).getByRole("button", { name: "关闭 Copilot" }));
  expect(panel).not.toBeVisible();
  fireEvent.click(open);
  expect(panel).toBeVisible();
  expect(created).toBe(1);
  expect(within(panel).getByText("You")).toBeVisible();
});

it("retains messages for view changes and isolates Job B from Job A including SSE disposal", async () => {
  const tree = (value: ResultCopilotContext) => <MemoryRouter><CopilotPage embedded context={value} /></MemoryRouter>;
  const rendered = render(tree(context));
  const input = await screen.findByLabelText("Message Copilot");
  fireEvent.change(input, { target: { value: "Job A question" } });
  await waitFor(() => expect(screen.getByLabelText("Send message")).toBeEnabled());
  fireEvent.click(screen.getByLabelText("Send message"));
  await screen.findByText("Job A question");
  const oldSource = FakeEventSource.instances[0];
  rendered.rerender(tree({ ...context, resultType: "table", artifact: "outputs/salt.all.csv", title: "Table" }));
  expect(screen.getByText("Job A question")).toBeVisible();
  expect(created).toBe(1);
  rendered.rerender(tree({ ...context, jobId: "job-b", analysisType: "dem" }));
  await waitFor(() => expect(created).toBe(2));
  expect(oldSource.close).toHaveBeenCalled();
  expect(screen.queryByText("Job A question")).not.toBeInTheDocument();
  expect(scopes.get("thread-2")).toEqual(["job-b"]);
  expect(screen.getByText("当前任务：job-b")).toBeVisible();
});

it("keeps general chat without inventing job context", async () => {
  render(<MemoryRouter><CopilotPage /></MemoryRouter>);
  await waitFor(() => expect(created).toBe(1));
  expect(scopes.get("thread-1")).toEqual([]);
  expect(screen.getByText("No job selected")).toBeVisible();
  expect(screen.queryByLabelText("当前结果上下文")).not.toBeInTheDocument();
});

it("shows API and malformed SSE errors while preserving the draft", async () => {
  render(<MemoryRouter><CopilotPage embedded context={context} /></MemoryRouter>);
  await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
  FakeEventSource.instances[0].dispatchEvent(new MessageEvent("message.created", { data: "invalid-json" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Could not read a Copilot update");
  const previous = fetchJson.getMockImplementation()!;
  fetchJson.mockImplementation(async (url, init) => { if (url.endsWith("/turns") && init?.method === "POST") throw new Error("offline"); return previous(url, init); });
  fireEvent.change(screen.getByLabelText("Message Copilot"), { target: { value: "Keep this question" } });
  await waitFor(() => expect(screen.getByLabelText("Send message")).toBeEnabled());
  fireEvent.click(screen.getByLabelText("Send message"));
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("Copilot is temporarily unavailable"));
  expect(screen.getByLabelText("Message Copilot")).toHaveValue("Keep this question");
});

it("recovers a broken SSE connection using the existing polling path", async () => {
  render(<MemoryRouter><CopilotPage embedded context={context} /></MemoryRouter>);
  await waitFor(() => expect(FakeEventSource.instances).toHaveLength(1));
  await screen.findByText("What are you investigating?");
  const before = fetchJson.mock.calls.filter(([url]) => url === "/api/agent/threads/thread-1").length;
  vi.useFakeTimers();
  act(() => FakeEventSource.instances[0].onerror?.(new Event("error")));
  expect(screen.getByTitle("Connection status")).toHaveTextContent("Reconnecting");
  await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
  expect(fetchJson.mock.calls.filter(([url]) => url === "/api/agent/threads/thread-1")).toHaveLength(before + 1);
  act(() => FakeEventSource.instances[0].onopen?.());
  expect(screen.getByTitle("Connection status")).toHaveTextContent("Live");
  const recovered = fetchJson.mock.calls.length;
  await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
  expect(fetchJson.mock.calls).toHaveLength(recovered);
});
