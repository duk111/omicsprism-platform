export class ApiRequestError extends Error {
  status: number;
  detail: unknown;
  requestId: string | null;

  constructor(message: string, status: number, detail: unknown, requestId: string | null = null) {
    super(message);
    this.name = "ApiRequestError";
    this.status = status;
    this.detail = detail;
    this.requestId = requestId;
  }
}

export async function apiFetch(input: RequestInfo | URL, init: RequestInit = {}) {
  const response = await fetch(input, {
    ...init,
    credentials: "include",
  });
  return response;
}

export async function apiFetchJson(input: RequestInfo | URL, init: RequestInit = {}) {
  const response = await apiFetch(input, init);
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const detail = body?.detail ?? body;
    const message = typeof detail === "string" ? detail : (detail?.user_message ?? detail?.message ?? "Request failed");
    throw new ApiRequestError(message, response.status, detail, response.headers.get("x-request-id"));
  }
  return response.json();
}
