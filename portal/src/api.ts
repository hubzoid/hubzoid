export type Me = { subject: string; org_admin: boolean; manageable: string[] };
export type Hub = { key: string; name: string; authoritative: boolean };
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
  perms: string[];
  inherited: string[];
  effective: string[];
  center?: string;
};
export type Access = {
  hub: string;
  authoritative: boolean;
  can_manage_admins: boolean;
  permissions: Permission[];
  rows: AccessRow[];
  total: number;
  public: boolean;
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
  display: string | null;
  owui_id: string | null;
  pending: number;
  blocked: boolean;
  organization_admin: boolean;
  access: Record<string, string[]>;
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

export async function request<T>(
  path: string,
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  const response = await fetch("/portal/api" + path, {
    credentials: "include",
    signal,
    method: body === undefined ? "GET" : "POST",
    headers:
      body === undefined ? undefined : { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) {
    let message = `${response.status} — Request failed`;
    try {
      const data = await response.json();
      message = typeof data.detail === "string" ? data.detail : message;
    } catch {
      /* use status */
    }
    throw new Error(message);
  }
  return response.json();
}
export function query(
  values: Record<string, string | number | boolean | undefined>,
) {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(values))
    if (v !== undefined && v !== "") q.set(k, String(v));
  return "?" + q.toString();
}
