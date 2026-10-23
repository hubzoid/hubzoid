// Fetch helpers for the Hubzoid web app API. Errors come back as
// {"detail": {"code", "message"}} (contract section 6); this turns every failure,
// including a dropped connection, into an ApiError with a code the UI can map
// to a plain sentence.

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly retryAfter?: number;
  /** The server's whole {"detail": {...}} object, for fields such as tries_left. */
  readonly detail?: Record<string, unknown>;
  constructor(status: number, code: string, message: string, retryAfter?: number,
              detail?: Record<string, unknown>) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.retryAfter = retryAfter;
    this.detail = detail;
  }
  /** True when the server gave a sentence written for people. */
  get hasServerMessage() {
    return this.message !== "" && this.message !== this.code;
  }
}

type Unauthorized = () => void;
let onUnauthorized: Unauthorized | null = null;

/** The app registers this so a session that ends mid-use goes back to sign-in. */
export function setUnauthorizedHandler(fn: Unauthorized | null) {
  onUnauthorized = fn;
}

export type RequestOptions = {
  method?: string;
  body?: unknown;
  signal?: AbortSignal;
  /** Skip the global "session ended" redirect (sign-in forms expect 401s). */
  quiet401?: boolean;
  headers?: Record<string, string>;
};

function codeForStatus(status: number): string {
  if (status === 401) return "unauthenticated";
  if (status === 403) return "forbidden";
  if (status === 404) return "not_found";
  if (status === 409) return "conflict";
  if (status === 413) return "too_large";
  if (status === 429) return "rate_limited";
  if (status >= 500) return "server_error";
  return "request_failed";
}

/** Build an ApiError from a failed response (body may be JSON or anything). */
export async function errorFrom(response: Response): Promise<ApiError> {
  let code = codeForStatus(response.status);
  let message = "";
  let retryAfter: number | undefined;
  let raw: Record<string, unknown> | undefined;
  const header = response.headers.get("retry-after");
  if (header && !Number.isNaN(Number(header))) retryAfter = Number(header);
  try {
    const data = await response.json();
    // Only the contract's {"detail": {"code", "message"}} carries a sentence
    // for people; a bare string ("Not Found") is framework boilerplate.
    const detail = data?.detail ?? data;
    if (detail && typeof detail === "object") {
      raw = detail;
      if (typeof detail.code === "string") code = detail.code;
      if (typeof detail.message === "string") message = detail.message;
      const ra = detail.retry_after ?? data?.retry_after;
      if (typeof ra === "number") retryAfter = ra;
    }
  } catch {
    /* not JSON */
  }
  return new ApiError(response.status, code, message || code, retryAfter, raw);
}

export function networkError(): ApiError {
  return new ApiError(0, "network", "network");
}

export function isAbort(error: unknown): boolean {
  return (
    (error instanceof DOMException && error.name === "AbortError") ||
    (error instanceof Error && error.name === "AbortError")
  );
}

/** The browser's time zone, sent with each request: schedules made in chat
 *  use it when the person has none set (HUB-140 D4). */
export const zoneHeader: Record<string, string> = (() => {
  try {
    const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
    const out: Record<string, string> = {};
    if (zone) out["x-client-timezone"] = zone;
    return out;
  } catch {
    return {};
  }
})();

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, signal, quiet401, headers = {} } = options;
  const init: RequestInit = {
    method,
    signal,
    credentials: "same-origin",
    headers: { accept: "application/json", ...zoneHeader, ...headers },
  };
  if (body instanceof FormData) init.body = body;
  else if (body !== undefined) {
    init.body = JSON.stringify(body);
    (init.headers as Record<string, string>)["content-type"] = "application/json";
  }
  let response: Response;
  try {
    response = await fetch(path, init);
  } catch (error) {
    if (isAbort(error)) throw error;
    throw networkError();
  }
  if (!response.ok) {
    const error = await errorFrom(response);
    if (response.status === 401 && !quiet401) onUnauthorized?.();
    throw error;
  }
  if (response.status === 204) return undefined as T;
  const text = await response.text();
  if (!text) return undefined as T;
  try {
    return JSON.parse(text) as T;
  } catch {
    return text as T;
  }
}

export const get = <T>(path: string, options?: RequestOptions) => request<T>(path, options);
export const post = <T>(path: string, body?: unknown, options?: RequestOptions) =>
  request<T>(path, { ...options, method: "POST", body });
export const patch = <T>(path: string, body?: unknown, options?: RequestOptions) =>
  request<T>(path, { ...options, method: "PATCH", body });
export const put = <T>(path: string, body?: unknown, options?: RequestOptions) =>
  request<T>(path, { ...options, method: "PUT", body });
export const del = <T>(path: string, options?: RequestOptions) =>
  request<T>(path, { ...options, method: "DELETE" });

/** Hub-scoped URL: `${agent.api_base}/api/...` ("" for a single hub). */
export function hubUrl(apiBase: string | undefined, path: string): string {
  const base = (apiBase || "").replace(/\/+$/, "");
  return `${base}${path}`;
}

export const enc = encodeURIComponent;
