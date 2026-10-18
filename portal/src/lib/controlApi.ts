// Calls to the 2.0 control-plane API (/api/chat/config, /api/branding,
// /api/admin, /api/parity). Errors arrive as {"detail": {"code", "message",
// ...}}; this keeps the code, the message and any extra fields (such as
// current_revision) for the screen to show.

export class ControlError extends Error {
  readonly status: number;
  readonly code: string;
  readonly detail: Record<string, unknown>;
  constructor(status: number, code: string, message: string, detail: Record<string, unknown>) {
    super(message);
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

export async function control<T>(
  path: string,
  options: { method?: string; body?: unknown; idempotencyKey?: string; raw?: BodyInit; headers?: Record<string, string> } = {},
): Promise<T> {
  const headers: Record<string, string> = { accept: "application/json", ...(options.headers ?? {}) };
  let body: BodyInit | undefined = options.raw;
  if (options.body !== undefined) {
    headers["content-type"] = "application/json";
    body = JSON.stringify(options.body);
  }
  if (options.idempotencyKey) headers["Idempotency-Key"] = options.idempotencyKey;
  let response: Response;
  try {
    response = await fetch(path, { method: options.method ?? (body ? "POST" : "GET"), credentials: "same-origin", headers, body });
  } catch {
    throw new ControlError(0, "network", "Can't reach Hubzoid. Check your connection and try again.", {});
  }
  if (!response.ok) {
    let detail: Record<string, unknown> = {};
    try {
      const data = await response.json();
      detail = typeof data?.detail === "object" && data.detail ? data.detail : { message: String(data?.detail ?? "") };
    } catch {
      /* not JSON */
    }
    throw new ControlError(
      response.status,
      String(detail.code ?? response.status),
      String(detail.message || `Request failed (${response.status}).`),
      detail,
    );
  }
  return (await response.json()) as T;
}

export function newKey(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}
