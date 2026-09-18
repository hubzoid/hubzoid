// A synthetic deployment behind the portal API. Everything here is invented:
// no customer service is contacted and no real access is changed. The
// handlers mirror hubzoid/portal.py + hubzoid/access/store.py semantics that
// the UI depends on (cascade on use_hub revoke, auto-grant of use_hub, org
// domain '*', last-admin protection, blocked subjects, role scoping).

const ORG = "*";
const EVERYONE = "*";
const USE_HUB = "use_hub";
const MANAGE_ACCESS = "manage_access";

const HOUR = 3600;
const NOW = Math.floor(Date.now() / 1000);
const iso = (secondsAgo) => new Date((NOW - secondsAgo) * 1000).toISOString();

function createFixture() {
  const state = {
    role: "org", // 'org' | 'hub' | 'user'
    viewer: "admin@example.org",
    mutations: [], // every POST body, in order
    delays: {}, // endpoint -> ms (e.g. { "/access": 700 })
    failNext: null, // { match: (endpoint, body) => boolean, status, detail }
    visibility: { state: "ok", updated: NOW - 40 },
    schedulerHeartbeat: iso(30),
    hubs: [
      { key: "finance", name: "Finance Assistant", authoritative: true },
      { key: "support", name: "Support Assistant", authoritative: true },
      { key: "itops", name: "IT Ops Assistant", authoritative: false },
    ],
    catalogs: {
      finance: [
        perm(USE_HUB, "Use this agent", "Open the agent in the chat app and use its unrestricted tools."),
        perm(MANAGE_ACCESS, "Manage access", "Decide who can use this agent and what they can do."),
        perm("ledger", "Read ledger", "View accounting entries and balances."),
        perm("invoices", "Manage invoices", "Create, send and void customer invoices."),
        perm("payroll", "Run payroll", "Process employee payroll and view salaries.", true),
      ],
      support: [
        perm(USE_HUB, "Use this agent", "Open the agent in the chat app and use its unrestricted tools."),
        perm(MANAGE_ACCESS, "Manage access", "Decide who can use this agent and what they can do."),
        perm("tickets", "Work tickets", "Read and update helpdesk tickets."),
        perm("refunds", "Issue refunds", "Refund customer payments.", true),
      ],
      itops: [
        perm(USE_HUB, "Use this agent", "Open the agent in the chat app and use its unrestricted tools."),
        perm(MANAGE_ACCESS, "Manage access", "Decide who can use this agent and what they can do."),
        perm("servers", "Restart servers", "Restart production servers.", true),
      ],
    },
    identities: {
      "admin@example.org": { display: "Sam Whitfield", owui_id: "u_admin", pending: 0 },
      "aisha.rahman@example.org": { display: "Aisha Rahman", owui_id: "u_aisha", pending: 0 },
      "priya.natarajan@example.org": { display: "Priya Natarajan", owui_id: "u_priya", pending: 0 },
      "daniel.okafor@example.org": { display: null, owui_id: null, pending: 0 },
      "meilin.chen@example.org": { display: "Mei Lin Chen", owui_id: "u_meilin", pending: 1 },
      "tomas.herrera@example.org": { display: "Tomás Herrera", owui_id: "u_tomas", pending: 0 },
      "fin.admin@example.org": { display: "Finance Admin", owui_id: "u_finadmin", pending: 0 },
      "workflow:monthly_close": { display: null, owui_id: null, pending: 0 },
    },
    suspended: new Set(["tomas.herrera@example.org"]),
    grants: [
      ["admin@example.org", ORG, MANAGE_ACCESS],
      ["aisha.rahman@example.org", ORG, MANAGE_ACCESS],
      ["aisha.rahman@example.org", "finance", USE_HUB],
      ["priya.natarajan@example.org", "finance", USE_HUB],
      ["priya.natarajan@example.org", "finance", "ledger"],
      ["priya.natarajan@example.org", "finance", "invoices"],
      ["daniel.okafor@example.org", "finance", USE_HUB],
      ["workflow:monthly_close", "finance", USE_HUB],
      ["workflow:monthly_close", "finance", "ledger"],
      ["fin.admin@example.org", "finance", USE_HUB],
      ["fin.admin@example.org", "finance", MANAGE_ACCESS],
      ["meilin.chen@example.org", "support", USE_HUB],
      ["meilin.chen@example.org", "support", "tickets"],
      [EVERYONE, "support", USE_HUB],
    ],
    audit: [
      row(NOW - 2 * HOUR, "aisha.rahman@example.org", "grant", "priya.natarajan@example.org", "finance", "ledger"),
      row(NOW - 2 * HOUR + 1, "aisha.rahman@example.org", "grant", "priya.natarajan@example.org", "finance", USE_HUB),
      row(NOW - 5 * HOUR, "admin@example.org", "suspend", "tomas.herrera@example.org", ORG, null),
      row(NOW - 26 * HOUR, "admin@example.org", "grant", EVERYONE, "support", USE_HUB),
      row(NOW - 3 * 24 * HOUR, "bootstrap", "grant", "admin@example.org", ORG, MANAGE_ACCESS),
      row(NOW - 3 * 24 * HOUR, "bootstrap", "activate", null, "finance", null),
    ],
    decisions: {
      finance: [
        { ts: iso(20 * 60), user: "priya.natarajan@example.org", surface: "openwebui", tool: "ledger_read", decision: "allow", reason: "grant" },
        { ts: iso(45 * 60), user: "tomas.herrera@example.org", surface: "openwebui", tool: "payroll_run", decision: "deny", reason: "no-grant" },
        { ts: iso(3 * HOUR), user: "anonymous", surface: "system", tool: "ledger_read", decision: "deny", reason: "anonymous" },
      ],
      support: [
        { ts: iso(90 * 60), user: "meilin.chen@example.org", surface: "slack-dm", tool: "refund_issue", decision: "deny", reason: "surface:slack-dm" },
      ],
      itops: [],
    },
    workflows: {
      finance: [
        wf("finance", "monthly_close", "0 6 1 * *", "Asia/Kolkata"),
        wf("finance", "daily_ledger_check", "daily 06:00", "UTC"),
        wf("finance", "reissue_invoice", null, "UTC"),
      ],
      support: [wf("support", "ticket_digest", "weekly monday 08:00", "Europe/Berlin")],
      itops: [wf("itops", "patch_audit", "0 2 * * *", "UTC")],
    },
    runs: {
      "finance:monthly_close": [
        run("finance", "monthly_close", "mc-2026-09-01", "SUCCESS", NOW - 17 * 24 * HOUR, 84_000, "Closed August 2026: 412 entries reconciled, 0 exceptions.", null, [
          step("fetch_entries", 0, 12_000, "412 entries"),
          step("reconcile", 12_000, 80_000, "0 exceptions"),
          step("post_summary", 80_000, 84_000, "Posted to #finance"),
        ]),
        run("finance", "monthly_close", "mc-2026-08-01", "ERROR", NOW - 48 * 24 * HOUR, 9_000, null, "LedgerLockedError: period 2026-07 is locked by another close", [
          step("fetch_entries", 0, 8_000, "398 entries"),
          step("reconcile", 8_000, 9_000, null, "LedgerLockedError: period 2026-07 is locked by another close"),
        ]),
      ],
      "finance:daily_ledger_check": [],
      "finance:reissue_invoice": [],
      "support:ticket_digest": [],
      "itops:patch_audit": [],
    },
  };

  const gs = {
    can(subject, hub, action) {
      if (!subject || state.suspended.has(subject)) return false;
      return state.grants.some(
        ([s, h, p]) => (s === subject || s === EVERYONE) && (h === hub || h === ORG) && (p === action || p === "*"),
      );
    },
    permissionsFor(subject, hub) {
      if (state.suspended.has(subject)) return [];
      return [...new Set(state.grants.filter(([s, h]) => (s === subject || s === EVERYONE) && (h === hub || h === ORG)).map(([, , p]) => p))].sort();
    },
    orgAdmins() {
      return state.grants.filter(([, h, p]) => h === ORG && p === MANAGE_ACCESS).map(([s]) => s);
    },
    audit(actor, action, subject, hub, permission) {
      state.audit.unshift(row(NOW, actor, action, subject, hub, permission));
    },
    grant(subject, hub, permission, actor) {
      const rows = [[subject, hub, permission]];
      if (hub !== ORG && permission !== USE_HUB) rows.push([subject, hub, USE_HUB]);
      for (const r of rows) {
        if (!state.grants.some(([s, h, p]) => s === r[0] && h === r[1] && p === r[2])) state.grants.push(r);
        gs.audit(actor, "grant", r[0], r[1], r[2]);
      }
      if (!state.identities[subject] && subject !== EVERYONE)
        state.identities[subject] = { display: null, owui_id: null, pending: 0 };
    },
    revoke(subject, hub, permission, actor) {
      const removesAdmin = hub === ORG && permission === MANAGE_ACCESS;
      if (removesAdmin) {
        const admins = gs.orgAdmins();
        if (admins.includes(subject) && admins.length <= 1) throw conflict("cannot remove the last org admin; grant another first");
      }
      if (hub !== ORG && permission === USE_HUB) {
        for (const [s, h, p] of state.grants) if (s === subject && h === hub && p !== USE_HUB) gs.audit(actor, "revoke", s, h, p);
        state.grants = state.grants.filter(([s, h]) => !(s === subject && h === hub));
      } else {
        state.grants = state.grants.filter(([s, h, p]) => !(s === subject && h === hub && p === permission));
      }
      gs.audit(actor, "revoke", subject, hub, permission);
    },
  };

  function admin() {
    if (state.role === "user") return null;
    if (state.role === "hub") return { subject: "fin.admin@example.org", org: false, manageable: ["finance"] };
    return { subject: state.viewer, org: true, manageable: state.hubs.map((h) => h.key) };
  }
  const allowedHubs = (a) => state.hubs.filter((h) => a.org || a.manageable.includes(h.key));
  const requireHub = (a, hub) => {
    if (!a.org && !a.manageable.includes(hub)) throw error(403, `Cannot manage ${hub}`);
    if (!state.hubs.some((h) => h.key === hub)) throw error(404, "Hub is not registered in this deployment");
  };
  const identityStatus = (subject) => {
    const id = state.identities[subject] || {};
    if (state.suspended.has(subject)) return "blocked";
    if (subject === EVERYONE) return "everyone";
    if (subject.startsWith("workflow:")) return "service";
    if (!id.owui_id) return "awaiting-signup";
    if (id.pending) return "pending-approval";
    return "active";
  };

  async function handle(method, endpoint, params, body) {
    const a = admin();
    if (!a) throw error(403, "Sign in with an account allowed to manage agent access.");
    if (state.delays[endpoint]) await new Promise((r) => setTimeout(r, state.delays[endpoint]));
    if (method === "POST") {
      state.mutations.push({ endpoint, ...body });
      if (state.failNext && state.failNext.match(endpoint, body)) {
        const { status, detail } = state.failNext;
        state.failNext = null;
        throw error(status, detail);
      }
    }
    switch (endpoint) {
      case "/me":
        return { subject: a.subject, org_admin: a.org, manageable: allowedHubs(a).map((h) => h.key) };
      case "/hubs":
        return { hubs: allowedHubs(a) };
      case "/permissions":
        requireHub(a, params.hub);
        return { hub: params.hub, permissions: state.catalogs[params.hub] };
      case "/access": {
        const hub = params.hub;
        requireHub(a, hub);
        const rows = {};
        for (const [subject, domain, perm] of state.grants) {
          if (domain === hub || (domain === ORG && perm === MANAGE_ACCESS)) {
            const r = (rows[subject] ??= { subject, perms: [], inherited: [], kind: subject.startsWith("workflow:") ? "service" : "person" });
            r[domain === hub ? "perms" : "inherited"].push(perm);
          }
        }
        const q = (params.q || "").toLowerCase();
        const list = Object.values(rows)
          .map((r) => ({
            ...r,
            effective: gs.permissionsFor(r.subject, hub),
            display: state.identities[r.subject]?.display || r.subject,
            status: identityStatus(r.subject),
          }))
          .filter((r) => (r.subject + " " + r.display).toLowerCase().includes(q))
          .sort((x, y) => x.subject.localeCompare(y.subject));
        const offset = Number(params.offset || 0);
        const limit = Number(params.limit || 50);
        return {
          hub,
          editable: true,
          authoritative: state.hubs.find((h) => h.key === hub).authoritative,
          can_manage_admins: a.org,
          permissions: state.catalogs[hub],
          total: list.length,
          public: gs.can("__signed_in_preview__", hub, USE_HUB),
          rows: list.slice(offset, offset + limit),
        };
      }
      case "/access/grant":
      case "/access/revoke": {
        const revoke = endpoint.endsWith("revoke");
        const subject = String(body.subject || "").trim().toLowerCase();
        const hub = String(body.hub || "").trim().toLowerCase();
        const perm = String(body.permission || "").trim().toLowerCase();
        if (hub === ORG) {
          if (!a.org || perm !== MANAGE_ACCESS || subject === EVERYONE)
            throw error(403, "Only organization admins can manage organization administrators");
        } else {
          requireHub(a, hub);
          if (!state.catalogs[hub].some((p) => p.permission === perm)) throw error(422, "Unknown permission for this hub");
        }
        if (subject === EVERYONE && !(a.org && perm === USE_HUB)) throw error(403, "Only organization admins may change public hub access");
        if (!a.org && (perm === MANAGE_ACCESS || (revoke && perm === USE_HUB && gs.can(subject, hub, MANAGE_ACCESS))))
          throw error(403, "Only organization admins may change administrator access");
        if (revoke) gs.revoke(subject, hub, perm, a.subject);
        else {
          if (state.suspended.has(subject)) throw error(409, "Reactivate this user before granting access");
          gs.grant(subject, hub, perm, a.subject);
        }
        return { ok: true };
      }
      case "/people": {
        const scopes = allowedHubs(a).map((h) => h.key);
        const visible = new Set(state.grants.filter(([, h]) => a.org || scopes.includes(h)).map(([s]) => s));
        const q = (params.q || "").toLowerCase();
        const rows = [];
        for (const [subject, id] of Object.entries(state.identities).sort()) {
          if (!a.org && !visible.has(subject)) continue;
          if (!(subject + " " + (id.display || "")).toLowerCase().includes(q)) continue;
          rows.push({
            subject,
            email: subject.includes("@") ? subject : null,
            display: id.display,
            owui_id: id.owui_id,
            pending: id.pending,
            blocked: state.suspended.has(subject),
            organization_admin: gs.can(subject, ORG, MANAGE_ACCESS),
            access: Object.fromEntries(scopes.map((h) => [h, gs.permissionsFor(subject, h)])),
          });
        }
        const offset = Number(params.offset || 0);
        const limit = Number(params.limit || 50);
        return { people: rows.slice(offset, offset + limit), total: rows.length };
      }
      case "/people/refresh":
        if (!a.org) throw error(403, "Organization admin required");
        return { ok: true, count: Object.values(state.identities).filter((i) => i.owui_id).length };
      case "/people/block": {
        if (!a.org) throw error(403, "Organization admin required");
        const subject = String(body.subject || "").trim().toLowerCase();
        if (!subject || subject === EVERYONE) throw error(409, "a person or service is required");
        const suspended = body.suspended !== false;
        const admins = gs.orgAdmins();
        if (suspended && admins.includes(subject) && admins.length <= 1) throw error(409, "cannot suspend the last org admin");
        if (suspended) state.grants = state.grants.filter(([s]) => s !== subject);
        if (suspended) state.suspended.add(subject);
        else state.suspended.delete(subject);
        gs.audit(a.subject, suspended ? "suspend" : "reactivate", subject, ORG, null);
        return { ok: true };
      }
      case "/workflows": {
        const hubs = params.hub ? [params.hub] : allowedHubs(a).map((h) => h.key);
        if (params.hub) requireHub(a, params.hub);
        return { workflows: hubs.flatMap((h) => state.workflows[h].map((w) => decorate(w, state))) };
      }
      case "/runs": {
        requireHub(a, params.hub);
        let runs = state.runs[`${params.hub}:${params.workflow}`] || [];
        if (params.run_id) runs = runs.filter((r) => r.id === params.run_id);
        const offset = Number(params.offset || 0);
        const limit = Number(params.limit || 50);
        return {
          runs: runs.slice(offset, offset + limit).map((r) => (params.run_id ? r : { ...r, steps: undefined })),
        };
      }
      case "/audit": {
        const hubs = params.hub ? [params.hub] : allowedHubs(a).map((h) => h.key);
        if (params.hub) requireHub(a, params.hub);
        let rows = hubs.flatMap((h) => state.decisions[h].map((d) => ({ ...d, hub: h })));
        if (params.user) rows = rows.filter((r) => r.user === params.user.toLowerCase());
        if (params.denied === "true") rows = rows.filter((r) => r.decision === "deny");
        rows.sort((x, y) => (x.ts < y.ts ? 1 : -1));
        const offset = Number(params.offset || 0);
        const limit = Number(params.limit || 50);
        return { rows: rows.slice(offset, offset + limit) };
      }
      case "/access-changes": {
        const scopes = params.hub ? [params.hub] : allowedHubs(a).map((h) => h.key);
        if (params.hub) requireHub(a, params.hub);
        if (a.org && !params.hub) scopes.push(ORG);
        let rows = state.audit.filter((r) => scopes.includes(r.hub));
        if (params.user) rows = rows.filter((r) => r.subject === params.user.toLowerCase());
        const offset = Number(params.offset || 0);
        const limit = Number(params.limit || 50);
        return { rows: rows.slice(offset, offset + limit) };
      }
      case "/sync":
        if (!a.org) throw error(403, "Organization admin required");
        state.visibility = { state: "ok", models: 2, updated: NOW };
        return state.visibility;
      case "/overview": {
        const hs = allowedHubs(a);
        const keys = new Set(hs.map((h) => h.key));
        const grants = state.grants.filter(([, h]) => keys.has(h));
        const managed = hs.filter((h) => h.authoritative).length;
        return {
          hubs: hs.length,
          grants: grants.length,
          people: new Set(grants.filter(([s]) => s !== EVERYONE).map(([s]) => s)).size,
          authoritative: managed === hs.length,
          managed,
          legacy: hs.length - managed,
          visibility: state.visibility,
        };
      }
      default:
        throw error(404, "Not found");
    }
  }

  return { state, handle };
}

function perm(permission, label, description, sensitive = false) {
  return { permission, label, description, sensitive };
}
function row(ts, actor, action, subject, hub, permission) {
  return { ts, actor, action, subject, hub, permission };
}
function wf(hub, name, schedule, timezone) {
  return { hub, name, schedule, timezone, error: null, source: `workflows/${name}/main.py` };
}
function decorate(w, state) {
  const stale = w.hub === "itops"; // the IT Ops bridge has no recent heartbeat
  const enabled = !!w.schedule;
  return {
    ...w,
    enabled,
    state: !w.schedule ? "manual" : stale ? "stale" : "scheduled",
    next_run: w.schedule && !stale ? new Date((NOW + 6 * HOUR) * 1000).toISOString() : null,
    last_dispatch: w.schedule ? new Date((NOW - 18 * HOUR) * 1000).toISOString() : null,
    missed: stale ? 2 : 0,
    heartbeat: stale ? new Date((NOW - 26 * HOUR) * 1000).toISOString() : state.schedulerHeartbeat,
    downtime: stale ? { since: new Date((NOW - 26 * HOUR) * 1000).toISOString(), until: new Date((NOW - 2 * HOUR) * 1000).toISOString(), missed: 2 } : null,
  };
}
function run(hub, name, id, status, startedSeconds, durationMs, output, err, steps) {
  const started = startedSeconds * 1000;
  return {
    hub,
    id,
    name,
    status,
    started,
    completed: started + durationMs,
    duration_ms: durationMs,
    output,
    error: err,
    steps: steps.map((s) => ({ ...s, started: started + s.started, completed: started + s.completed })),
  };
}
function step(name, started, completed, output, err = null) {
  return { name, started, completed, output, error: err };
}
function error(status, detail) {
  const e = new Error(detail);
  e.status = status;
  e.detail = detail;
  return e;
}
const conflict = (detail) => error(409, detail);

module.exports = { createFixture };
