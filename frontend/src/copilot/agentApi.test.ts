import { afterEach, describe, expect, it, vi } from "vitest";
import { agentApi } from "./agentApi";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("agentApi.resumeTurn", () => {
  it("sends the run idempotency key only as a request header", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse());
    vi.stubGlobal("fetch", fetchMock);

    await agentApi.resumeTurn("thread/1", "turn/1", {
      kind: "confirmation",
      interrupt_id: "interrupt-1",
      plan_id: "plan-1",
      plan_version: 1,
      approve: true,
    });

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/agent/threads/thread%2F1/turns/turn%2F1/resume");
    expect(init.headers).toMatchObject({ "Content-Type": "application/json", "Idempotency-Key": expect.any(String) });
    expect(JSON.parse(init.body as string)).toEqual({
      kind: "confirmation", interrupt_id: "interrupt-1", plan_id: "plan-1",
      plan_version: 1, approve: true,
    });
  });

  it("does not add an idempotency key to non-run resumes", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse());
    vi.stubGlobal("fetch", fetchMock);

    await agentApi.resumeTurn("thread-1", "turn-1", {
      kind: "clarification",
      interrupt_id: "interrupt-1",
      answer: "salt",
    });

    const init = fetchMock.mock.calls[0][1] as RequestInit;
    expect(init.headers).toEqual({ "Content-Type": "application/json" });
  });
});

describe("agentApi.saveFeedback", () => {
  it("sends only the public structured feedback payload", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse());
    vi.stubGlobal("fetch", fetchMock);

    await agentApi.saveFeedback("thread/1", "assistant/1", {
      rating: "unhelpful", failure_category: "bad_plan", correction_text: "Use paired samples.",
    });

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/agent/threads/thread%2F1/messages/assistant%2F1/feedback");
    expect(init.method).toBe("PUT");
    expect(JSON.parse(init.body as string)).toEqual({
      rating: "unhelpful", failure_category: "bad_plan", correction_text: "Use paired samples.",
    });
  });
});

describe("agentApi.listTrace", () => {
  it("requests the bounded trace projection for one thread", async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse());
    vi.stubGlobal("fetch", fetchMock);

    await agentApi.listTrace("thread/1", 25);

    expect(fetchMock.mock.calls[0][0]).toBe("/api/agent/threads/thread%2F1/trace?limit=25");
  });
});

function jsonResponse() {
  return new Response(JSON.stringify({}), { status: 200, headers: { "Content-Type": "application/json" } });
}
