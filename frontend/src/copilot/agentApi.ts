import { apiFetch, apiFetchJson, apiUrl } from "../api";
import { createClientId } from "../clientId";
import type {
  AgentInputBundleResponse,
  AgentMessageListResponse,
  AgentThreadDetailResponse,
  AgentThreadListResponse,
  AgentThreadResponse,
  AgentTurnListResponse,
  AgentTurnResponse,
  GraphClarificationResumeRequest,
  GraphConfirmationResumeRequest,
  GraphTurnResult,
} from "../api-types";

const root = "/api/agent/threads";

export type GraphResumeRequest = GraphClarificationResumeRequest | GraphConfirmationResumeRequest;

function json(method: string, body: unknown): RequestInit {
  return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

export const agentApi = {
  listThreads: () => apiFetchJson(root) as Promise<AgentThreadListResponse>,
  createThread: (focusJobIds: string[] = []) =>
    apiFetchJson(root, json("POST", { focus_job_ids: focusJobIds })) as Promise<AgentThreadResponse>,
  getThread: (threadId: string) =>
    apiFetchJson(`${root}/${encodeURIComponent(threadId)}`) as Promise<AgentThreadDetailResponse>,
  listMessages: (threadId: string, after?: string) =>
    apiFetchJson(`${root}/${encodeURIComponent(threadId)}/messages${after ? `?after=${encodeURIComponent(after)}` : ""}`) as Promise<AgentMessageListResponse>,
  listTurns: (threadId: string, after?: string) =>
    apiFetchJson(`${root}/${encodeURIComponent(threadId)}/turns${after ? `?after=${encodeURIComponent(after)}` : ""}`) as Promise<AgentTurnListResponse>,
  createTurn: (threadId: string, message: string, inputBundleId: string | null, focusJobIds: string[]) =>
    apiFetchJson(`${root}/${encodeURIComponent(threadId)}/turns`, {
      ...json("POST", { message, input_bundle_id: inputBundleId, focus_job_ids: focusJobIds }),
      headers: { "Content-Type": "application/json", "Idempotency-Key": createClientId() },
    }) as Promise<AgentTurnResponse | GraphTurnResult>,
  resumeTurn: (threadId: string, checkpointTurnId: string, request: GraphResumeRequest) => {
    const run = request.kind === "confirmation" && request.action === "run";
    return apiFetchJson(
      `${root}/${encodeURIComponent(threadId)}/turns/${encodeURIComponent(checkpointTurnId)}/resume`,
      {
        ...json("POST", request),
        headers: {
          "Content-Type": "application/json",
          ...(run ? { "Idempotency-Key": createClientId() } : {}),
        },
      },
    ) as Promise<GraphTurnResult>;
  },
  cancelTurn: (threadId: string, turnId: string) =>
    apiFetchJson(`${root}/${encodeURIComponent(threadId)}/turns/${encodeURIComponent(turnId)}/cancel`, { method: "POST" }) as Promise<AgentTurnResponse>,
  deleteThread: (threadId: string) =>
    apiFetch(`${root}/${encodeURIComponent(threadId)}`, { method: "DELETE" }),
  uploadBundle: async (threadId: string, attachments: { file: File; field: string }[]) => {
    const body = new FormData();
    attachments.forEach(({ file, field }) => { body.append("files", file); body.append("fields", field); });
    return apiFetchJson(`${root}/${encodeURIComponent(threadId)}/input-bundles`, { method: "POST", body }) as Promise<AgentInputBundleResponse>;
  },
  streamUrl: (threadId: string) => apiUrl(`${root}/${encodeURIComponent(threadId)}/stream`),
  ping: () => apiFetch("/health"),
};

export function isGraphTurnResult(
  result: AgentTurnResponse | GraphTurnResult,
): result is GraphTurnResult {
  return "checkpoint_turn_id" in result;
}
