// Typed client for the Hubzoid portal JSON API (served at /portal/api).
const BASE = "/portal/api";

async function get<T>(path: string): Promise<T> {
  const r = await fetch(BASE + path, { credentials: "include" });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}
async function post<T>(path: string, body: unknown): Promise<T> {
  const r = await fetch(BASE + path, {
    method: "POST",
    credentials: "include",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

export type Me = { subject: string; org_admin: boolean; manageable: string[] };
export type Hub = { key: string; name: string; perms: string[] };
export type AccessRow = { subject: string; kind: string; perms: string[]; center?: string | null };
export type Access = { hub: string; editable: boolean; permissions: string[]; rows: AccessRow[] };
export type Perm = { permission: string; prod: boolean };
export type Workflow = { name: string; schedule: string | null; timezone: string | null };
export type AuditRow = Record<string, string>;
export type Overview = { hubs: number; grants: number; people: number; authoritative: boolean };

export const api = {
  me: () => get<Me>("/me"),
  hubs: () => get<{ hubs: Hub[] }>("/hubs").then((d) => d.hubs),
  overview: () => get<Overview>("/overview"),
  permissions: (hub: string) => get<{ permissions: Perm[] }>(`/permissions?hub=${encodeURIComponent(hub)}`).then((d) => d.permissions),
  access: (hub: string) => get<Access>(`/access?hub=${encodeURIComponent(hub)}`),
  grant: (subject: string, hub: string, permission: string) => post("/access/grant", { subject, hub, permission }),
  revoke: (subject: string, hub: string, permission: string) => post("/access/revoke", { subject, hub, permission }),
  workflows: () => get<{ workflows: Workflow[] }>("/workflows").then((d) => d.workflows),
  audit: (opts: { limit?: number; user?: string; denied?: boolean } = {}) => {
    const q = new URLSearchParams();
    if (opts.limit) q.set("limit", String(opts.limit));
    if (opts.user) q.set("user", opts.user);
    if (opts.denied) q.set("denied", "1");
    return get<{ rows: AuditRow[] }>(`/audit?${q}`).then((d) => d.rows);
  },
};
