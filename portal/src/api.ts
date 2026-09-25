export type Me = { subject: string; org_admin: boolean; manageable: string[] };
export type Hub = { key: string; name: string; model_id?: string; can_chat?: boolean; authoritative: boolean };
export type Permission = {
  permission: string;
  label: string;
  description: string;
  sensitive: boolean;
};
export type AccessRow = {
  subject: string;
  display: string;
  kind: string;
  status: string;
  // Block markers, kept separate so the editor can explain an admin block vs a
  // missing chat account (both surface as status "blocked").
  suspended?: boolean;
  account_unavailable?: boolean;
  perms: string[];
  inherited: string[];
  effective: string[];
  center?: string;
};
export type Access = {
  hub: string;
  authoritative: boolean;
  // False for a legacy (un-migrated) hub: its access is read-only here and still
  // governed by the chat app; the API refuses edits until the hub is migrated.
  editable: boolean;
  can_manage_admins: boolean;
  permissions: Permission[];
  rows: AccessRow[];
  total: number;
  public: boolean;
  // Policy revision at load time — sent back with the first save so the backend
  // can reject an edit built on access another admin has since changed.
  revision: number;
};
export type Workflow = {
  hub: string;
  name: string;
  schedule: string | null;
  timezone: string;
  state: string;
  error: string | null;
  next_run: string | null;
  last_dispatch: string | null;
  missed: number;
  heartbeat: string | null;
  downtime: { since: string; until: string; missed: number } | null;
};
export type Run = {
  hub: string;
  id: string;
  name: string;
  status: string;
  started: number | null;
  completed: number | null;
  duration_ms: number | null;
  output: string | null;
  error: string | null;
  steps?: {
    name: string;
    started: number | null;
    completed: number | null;
    output: string | null;
    error: string | null;
  }[];
};
export type Person = {
  subject: string;
  email?: string | null;
  display: string | null;
  owui_id: string | null;
  pending: number | boolean;
  blocked: boolean;
  // The backend's authoritative single status, plus the two separate block
  // markers so the UI can tell an admin block from an unavailable chat account.
  status: string;
  suspended: boolean;
  account_unavailable: boolean;
  organization_admin: boolean;
  access: Record<string, string[]>;
};

// The /people/block response carries the resulting state and a plain-language
// message (e.g. reactivation that leaves access paused because the chat account
// is gone). The UI must announce that, not a blanket "active again".
export type BlockResult = {
  ok: boolean;
  subject: string;
  changed: boolean;
  message?: string | null;
  status: string;
  suspended: boolean;
  account_unavailable: boolean;
  blocked: boolean;
};
export type AuditRow = {
  ts: number | string;
  hub: string;
  actor?: string;
  action?: string;
  subject?: string;
  permission?: string;
  user?: string;
  decision?: string;
  tool?: string;
  reason?: string;
  surface?: string;
};
export type Sync = {
  state: string;
  models?: number;
  updated?: number;
  error?: string;
};
export type Overview = {
  hubs: number;
  people: number;
  grants: number;
  managed: number;
  legacy: number;
  visibility: Sync;
};

export type SummaryHub = {
  key: string;
  name: string;
  managed: boolean;
  chats: number;
  messages: number;
  active_users: number;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number | null;
  unpriced: number;
  last_activity: number | null;
  users_with_access: number | null;
  everyone: boolean | null;
  denials: number;
  has_workflows: boolean;
  runs: number | null;
  failed: number | null;
  missed: number | null;
};
export type Summary = {
  period: "24h" | "7d" | "30d";
  since: number;
  generated: number;
  recording_since: number | null;
  has_workflows: boolean;
  runs_available: boolean;
  totals: {
    chats: number;
    messages: number;
    active_users: number;
    input_tokens: number;
    output_tokens: number;
    cost_usd: number | null;
    unpriced: number;
    denials: number;
    runs: number | null;
    failed: number | null;
    missed: number | null;
  };
  hubs: SummaryHub[];
};

// status 0 means the request never got a response (network/abort): the server
// may or may not have applied it. `certain` is true only when we have a response
// that tells us the outcome definitively — a 4xx client rejection means nothing
// was committed; a 5xx or no-response is uncertain (an atomic write may have
// committed just before the connection dropped).
export class ApiError extends Error {
  status: number;
  certain: boolean;
  constructor(message: string, status: number) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.certain = status >= 400 && status < 500;
  }
}

export async function request<T>(
  path: string,
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  let response: Response;
  try {
    response = await fetch("/portal/api" + path, {
      credentials: "include",
      signal,
      method: body === undefined ? "GET" : "POST",
      headers:
        body === undefined ? undefined : { "content-type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (e) {
    // Network failure / abort — no response at all.
    throw new ApiError(
      e instanceof Error && e.message ? e.message : "Network error",
      0,
    );
  }
  if (!response.ok) {
    let message = `${response.status} — Request failed`;
    try {
      const data = await response.json();
      message = typeof data.detail === "string" ? data.detail : message;
    } catch {
      /* use status */
    }
    throw new ApiError(message, response.status);
  }
  return response.json();
}
export function query(
  values: Record<string, string | number | boolean | undefined>,
) {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(values))
    if (v !== undefined && v !== "") q.set(k, String(v));
  const s = q.toString();
  return s ? "?" + s : "";
}
