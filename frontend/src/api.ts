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

const API_BASE_PATH = normalizeBasePath(import.meta.env.VITE_API_BASE_PATH || "/api");
const PUBLIC_BASE_PATH = normalizeBasePath(import.meta.env.BASE_URL || import.meta.env.VITE_PUBLIC_BASE_PATH || "/");

export function publicUrl(path: string): string {
  const cleanPath = path.startsWith("/") ? path : `/${path}`;
  if (!PUBLIC_BASE_PATH) return cleanPath;
  if (cleanPath === PUBLIC_BASE_PATH || cleanPath.startsWith(`${PUBLIC_BASE_PATH}/`)) {
    return cleanPath;
  }
  return `${PUBLIC_BASE_PATH}${cleanPath}`;
}

export function publicRelativePath(): string {
  const path = window.location.pathname;
  if (!PUBLIC_BASE_PATH) return path;
  if (path === PUBLIC_BASE_PATH) return "/";
  if (path.startsWith(`${PUBLIC_BASE_PATH}/`)) {
    return path.slice(PUBLIC_BASE_PATH.length) || "/";
  }
  return path;
}

export function apiUrl(path: string): string {
  const cleanPath = path.startsWith("/") ? path : `/${path}`;
  if (!API_BASE_PATH) return cleanPath;
  if (cleanPath === API_BASE_PATH || cleanPath.startsWith(`${API_BASE_PATH}/`)) {
    return cleanPath;
  }
  if (cleanPath === "/api") return API_BASE_PATH;
  if (cleanPath.startsWith("/api/")) return `${API_BASE_PATH}${cleanPath.slice(4)}`;
  return `${API_BASE_PATH}${cleanPath}`;
}

export function assetUrl(path: string | null | undefined): string {
  if (!path) return "";
  if (/^(https?:)?\/\//.test(path) || path.startsWith("data:")) return path;
  return apiUrl(path);
}

export async function apiFetch(input: RequestInfo | URL, init: RequestInit = {}) {
  const normalizedInput = typeof input === "string" ? apiUrl(input) : input;
  const response = await fetch(normalizedInput, {
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

function normalizeBasePath(value: string): string {
  const trimmed = value.trim();
  if (!trimmed || trimmed === "/") return "";
  return `/${trimmed.replace(/^\/+|\/+$/g, "")}`;
}
