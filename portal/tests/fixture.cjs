// A synthetic deployment behind the portal API. Everything here is invented:
// no customer service is contacted and no real access is changed. The
// handlers mirror hubzoid/portal.py + hubzoid/access/store.py semantics that
// the UI depends on (cascade on use_hub revoke, auto-grant of use_hub, org
// domain '*', last-admin protection, blocked subjects, role scoping), and the
// account service (hubzoid/access/service.py): a duplicate is refused, an
// account created without its access is reported as such, and granting to an
// existing account never creates one.

const ORG = "*";
const EVERYONE = "*";
const USE_HUB = "use_hub";
const MANAGE_ACCESS = "manage_access";
const LEGACY_MSG =
  "This agent's access is still managed in the chat app — it has not been migrated to the dashboard.";

const HOUR = 3600;
const NOW = Math.floor(Date.now() / 1000);
const iso = (secondsAgo) => new Date((NOW - secondsAgo) * 1000).toISOString();

// Mirrors hubzoid/workflows/observe.STATUS_BUCKETS: a UI status filter maps to the
// concrete DBOS states it covers, applied server-side before pagination.
const STATUS_BUCKETS = {
  succeeded: ["SUCCESS"],
  failed: ["ERROR", "MAX_RECOVERY_ATTEMPTS_EXCEEDED"],
  running: ["PENDING", "ENQUEUED"],
  cancelled: ["CANCELLED"],
};
const KNOWN_STATUSES = new Set([
  "PENDING", "SUCCESS", "ERROR", "MAX_RECOVERY_ATTEMPTS_EXCEEDED", "CANCELLED", "ENQUEUED", "DELAYED",
]);
// Mirrors observe.resolve_statuses: absent → no filter; a supplied but unrecognized
// value is a 422, never silently dropped (which would widen the query).
function resolveStatuses(value) {
  if (!value) return null;
  const tokens = String(value).split(",").map((t) => t.trim()).filter(Boolean);
  if (!tokens.length) return null;
  const out = [];
  for (const t of tokens) {
    if (STATUS_BUCKETS[t]) out.push(...STATUS_BUCKETS[t]);
    else if (KNOWN_STATUSES.has(t.toUpperCase())) out.push(t.toUpperCase());
    else throw error(422, "Unknown run status filter. Use succeeded, failed, running or cancelled.");
  }
  return [...new Set(out)];
}

function createFixture() {
  const state = {
    role: "org", // 'org' | 'hub' | 'user'
    viewer: "admin@example.org",
    mutations: [], // every POST body, in order
    delays: {}, // endpoint -> ms (e.g. { "/access": 700 })
    failNext: null, // { match: (endpoint, body) => boolean, status, detail }
    failNextGet: null, // { endpoint, status, detail } — fail the next matching GET once
    visibility: { state: "ok", updated: NOW - 40 },
    schedulerHeartbeat: iso(30),
    hubs: [
      { key: "finance", name: "Finance Assistant", authoritative: true },
      { key: "support", name: "Support Assistant", authoritative: true },
      { key: "itops", name: "IT Ops Assistant", authoritative: false },
    ],
    catalogs: {
      finance: [
        perm("curator", "Save shared knowledge", "Use remember to create or replace shared agent knowledge."),
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
    // Accounts that vanished from the chat-app directory (deleted/renamed).
    // Blocked in effect, but NOT an admin suspension — reactivate can't fix it.
    unavailable: new Set(),
    // How new accounts can sign in (GET /me sign_in).
    signIn: { password: true, google: false },
    // Accounts that exist in the chat app but are not recorded here yet
    // (email -> name), e.g. created by an answer that was lost.
    chatOnly: new Map(),
    // Every account the chat app was asked to create, in order.
    accountsCreated: [],
    // One-shot faults for POST /accounts: "partial" (created, access not
    // saved) or "lost" (created, but the answer never arrived).
    accountFault: null,
    revision: 0, // policy revision; every grant/revoke/block bumps it
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
      "finance:daily_ledger_check": [
        run("finance", "daily_ledger_check", "dlc-2026-09-18", "SUCCESS", NOW - 6 * HOUR, 3_200, "No exceptions.", null, [
          step("scan", 0, 3_200, "ok"),
        ]),
      ],
      "finance:reissue_invoice": [
        // A currently-running (PENDING) workflow: completed/duration are null, so
        // auto-refresh and the "running" status filter have something to catch.
        running("finance", "reissue_invoice", "ri-2026-09-18", NOW - 2 * 60),
      ],
      "support:ticket_digest": [],
      // A second support workflow with history, so the cross-agent Runs view spans
      // more than one agent and more than one workflow name.
      "support:sla_report": [
        run("support", "sla_report", "td-2026-09-15", "SUCCESS", NOW - 3 * 24 * HOUR, 5_000, "Digest sent: 37 open tickets.", null, [
          step("collect", 0, 4_000, "37 tickets"),
          step("send", 4_000, 5_000, "Posted to #support"),
        ]),
        run("support", "sla_report", "td-2026-09-08", "CANCELLED", NOW - 10 * 24 * HOUR, 1_000, null, null, []),
      ],
      // Only visible to org admins; a hub admin scoped to finance must never see it.
      "itops:patch_audit": [
        run("itops", "patch_audit", "pa-2026-09-17", "ERROR", NOW - 30 * HOUR, 2_000, null, "PatchFailed: node web-3 unreachable", [
          step("enumerate", 0, 1_000, "12 nodes"),
          step("apply", 1_000, 2_000, null, "PatchFailed: node web-3 unreachable"),
        ]),
      ],
    },
  };

  const gs = {
    can(subject, hub, action) {
      if (!subject || state.suspended.has(subject) || state.unavailable.has(subject)) return false;
      return state.grants.some(
        ([s, h, p]) => (s === subject || s === EVERYONE) && (h === hub || h === ORG) && (p === action || p === "*"),
      );
    },
    permissionsFor(subject, hub) {
      if (state.suspended.has(subject) || state.unavailable.has(subject)) return [];
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
      state.revision += 1;
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
      state.revision += 1;
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
    if (state.unavailable.has(subject)) return "blocked";
    return "active";
  };
  const accountFlags = (subject) => ({
    suspended: state.suspended.has(subject),
    account_unavailable: state.unavailable.has(subject),
    blocked: state.suspended.has(subject) || state.unavailable.has(subject),
  });

  // What an administrator may grant in a hub (service.ceiling): an org admin
  // every grantable capability; an agent manager what they hold there,
  // minus Manage access and admin-only capabilities.
  function ceiling(a, hub) {
    const entries = state.catalogs[hub].filter(grantable);
    if (a.org) return entries.map((p) => p.permission).sort();
    const held = gs.permissionsFor(a.subject, hub);
    return entries
      .filter((p) => p.delegate_grantable !== false && p.permission !== MANAGE_ACCESS && held.includes(p.permission))
      .map((p) => p.permission)
      .sort();
  }
  function checkGrants(a, subject, grants) {
    for (const g of grants) {
      const hub = String(g.hub || "").trim().toLowerCase();
      const perm = String(g.permission || "").trim().toLowerCase();
      if (hub === ORG) throw error(422, "Initial access is given per agent.", "invalid_grant");
      requireHub(a, hub);
      if (!state.hubs.find((h) => h.key === hub).authoritative) throw error(409, LEGACY_MSG, "legacy");
      if (!grantable(state.catalogs[hub].find((p) => p.permission === perm))) throw error(422, "Unknown permission for this hub", "unknown_permission");
      if (!a.org) {
        if (perm === MANAGE_ACCESS) throw error(403, "Only organization admins may change administrator access", "forbidden");
        if (subject === a.subject) throw error(403, "You can't change your own access. Ask an organization administrator.", "self_change");
        if (gs.can(subject, ORG, MANAGE_ACCESS)) throw error(403, "Only organization admins may change an organization administrator's access", "forbidden");
        if (!ceiling(a, hub).includes(perm)) throw error(403, `Outside your access: you can only grant or remove capabilities you hold in ${hub}.`, "outside_ceiling");
      }
    }
  }

  function catalogFor(hub) {
    const known = new Set(state.catalogs[hub].map((p) => p.permission));
    const stale = [...new Set(state.grants.filter(([, h, p]) => h === hub && !known.has(p)).map(([, , p]) => p))].sort();
    return [...state.catalogs[hub], ...stale.map(obsoletePerm)];
  }

  async function handle(method, endpoint, params, body) {
    const a = admin();
    if (!a) throw error(403, "Sign in with an account allowed to manage agent access.");
    if (state.delays[endpoint]) await new Promise((r) => setTimeout(r, state.delays[endpoint]));
    if (method === "GET" && state.failNextGet && state.failNextGet.endpoint === endpoint) {
      const { status, detail } = state.failNextGet;
      state.failNextGet = null;
      throw error(status, detail);
    }
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
        return {
          subject: a.subject,
          org_admin: a.org,
          manageable: allowedHubs(a).map((h) => h.key),
          grantable: Object.fromEntries(allowedHubs(a).map((h) => [h.key, h.authoritative ? ceiling(a, h.key) : []])),
          account_admin: a.org,
          can_create_accounts: a.org || allowedHubs(a).some((h) => h.authoritative),
          accounts_configured: true,
          sign_in: state.signIn,
        };
      case "/accounts": {
        if (method === "GET") {
          // Scoped like People: an agent manager sees their agents' people and
          // one account by its full email. Only real accounts are listed.
          const q = String(params.q || "").trim().toLowerCase();
          const scopes = allowedHubs(a).map((h) => h.key);
          const visible = new Set(state.grants.filter(([, h]) => scopes.includes(h)).map(([s]) => s));
          const rows = [];
          for (const [subject, id] of Object.entries(state.identities).sort()) {
            if (!id.owui_id || subject.startsWith("workflow:") || subject === EVERYONE) continue;
            if (!a.org && !visible.has(subject) && subject !== q) continue;
            if (q && !(subject + " " + (id.display || "")).toLowerCase().includes(q)) continue;
            rows.push({ subject, display: id.display, status: identityStatus(subject), ...accountFlags(subject), organization_admin: gs.can(subject, ORG, MANAGE_ACCESS) });
            if (rows.length >= Number(params.limit || 20)) break;
          }
          return { accounts: rows };
        }
        const email = String(body.email || "").trim().toLowerCase();
        const name = String(body.name || "").trim();
        const signIn = body.sign_in || "password";
        if (!EMAIL_RE.test(email)) throw error(422, "Enter a valid email address.", "invalid_email");
        if (!name) throw error(422, "Enter the person's name.", "invalid_name");
        if (signIn === "google") {
          if (!state.signIn.google) throw error(409, "Google sign-in isn't set up to attach to accounts on this deployment. Create the account with a password.", "google_unavailable");
          if (body.password) throw error(422, "An account that signs in with Google has no password to set.", "invalid_request");
        } else if (!body.password || String(body.password).length < 8) {
          throw error(422, "Use a password of at least 8 characters.", "invalid_password");
        }
        const grants = body.grants || [];
        if (!a.org && !grants.length) throw error(422, "Choose access in at least one agent you manage.", "grant_required");
        if (state.suspended.has(email)) throw error(409, "This person is blocked. Reactivate them under People first.", "blocked");
        const known = state.identities[email];
        if (known && known.owui_id && !state.unavailable.has(email))
          throw error(409, "An account with this email already exists. Grant access to it instead.", "account_exists");
        checkGrants(a, email, grants);
        // The chat app refuses a duplicate it holds but this deployment hasn't recorded.
        if (state.chatOnly.has(email))
          throw error(409, "An account with this email already exists. Grant access to it instead.", "account_exists");
        state.accountsCreated.push(email);
        const fault = state.accountFault;
        state.accountFault = null;
        if (fault === "lost") {
          state.chatOnly.set(email, name);
          throw error(503, "The chat app didn't confirm whether the account was created. Try again: if it was created, you'll be offered to grant access to it instead, and it won't be created twice.", "uncertain");
        }
        state.identities[email] = { display: name, owui_id: `u_${state.accountsCreated.length}`, pending: 0 };
        gs.audit(a.subject, "account_create", email, ORG, null);
        state.revision += 1;
        if (fault === "partial")
          throw error(502, `The account for ${email} was created, but access was not granted. Access could not be saved. Try again to grant access; the account won't be created twice.`,
            "partial", { account: { subject: email, name, sign_in: signIn }, access_granted: false, recorded: true, reason: "Access could not be saved." });
        const out = {};
        for (const g of grants) {
          gs.grant(email, g.hub, g.permission, a.subject);
          (out[g.hub] ??= []).push(g.permission);
        }
        return { ok: true, subject: email, owui_id: state.identities[email].owui_id, name, role: "user", sign_in: signIn, grants: out, revision: state.revision };
      }
      case "/accounts/grant": {
        // Access for an account that exists; never creates one or an email-only grant.
        const email = String(body.email || "").trim().toLowerCase();
        const grants = body.grants || [];
        if (!grants.length) throw error(422, "Choose the access to give.", "grant_required");
        checkGrants(a, email, grants);
        let id = state.identities[email];
        if (!id || !id.owui_id) {
          if (!state.chatOnly.has(email)) throw error(404, "No account uses this email. Create a new account instead.", "no_account");
          id = state.identities[email] = { display: state.chatOnly.get(email), owui_id: `u_bound_${email}`, pending: 0 };
          state.chatOnly.delete(email);
        }
        if (state.suspended.has(email)) throw error(409, "Reactivate this user before granting access", "blocked");
        if (state.unavailable.has(email)) throw error(409, "This account is unavailable in the chat app.", "unavailable");
        const out = {};
        for (const g of grants) {
          gs.grant(email, g.hub, g.permission, a.subject);
          (out[g.hub] ??= []).push(g.permission);
        }
        return { ok: true, subject: email, name: id.display, grants: out };
      }
      case "/hubs":
        return { hubs: allowedHubs(a) };
      case "/permissions":
        requireHub(a, params.hub);
        return { hub: params.hub, permissions: catalogFor(params.hub) };
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
          editable: !!state.hubs.find((h) => h.key === hub).authoritative,
          authoritative: state.hubs.find((h) => h.key === hub).authoritative,
          can_manage_admins: a.org,
          permissions: catalogFor(hub),
          total: list.length,
          public: gs.can("__signed_in_preview__", hub, USE_HUB),
          public_reliant: Object.entries(state.identities).filter(([subject, id]) =>
            id.owui_id && !id.pending && !subject.startsWith("workflow:") && !state.suspended.has(subject) &&
            !state.grants.some(([s, h, p]) => s === subject && h === hub && p === USE_HUB)).length,
          revision: state.revision,
          grantable: state.hubs.find((h) => h.key === hub).authoritative ? ceiling(a, hub) : [],
          viewer: a.subject,
          rows: list.slice(offset, offset + limit),
        };
      }
      case "/access/grant":
      case "/access/revoke": {
        const revoke = endpoint.endsWith("revoke");
        const subject = String(body.subject || "").trim().toLowerCase();
        const hub = String(body.hub || "").trim().toLowerCase();
        const perm = String(body.permission || "").trim().toLowerCase();
        if (body.expected_revision != null && body.expected_revision !== state.revision)
          throw error(409, "Access changed since you loaded it — someone else edited it. Reload and review the current access before saving.");
        if (hub === ORG) {
          if (!a.org || perm !== MANAGE_ACCESS || subject === EVERYONE)
            throw error(403, "Only organization admins can manage organization administrators");
        } else {
          requireHub(a, hub);
          if (!state.hubs.find((h) => h.key === hub).authoritative) throw error(409, LEGACY_MSG);
          const entry = state.catalogs[hub].find((p) => p.permission === perm);
          const held = state.grants.some(([s, h, p]) => s === subject && h === hub && p === perm);
          if (!grantable(entry) && !(revoke && held)) throw error(422, "Unknown permission for this hub");
        }
        // Mirrors AccessService: nobody creates new access for everyone signed in;
        // an organization administrator may remove an existing one.
        if (subject === EVERYONE && !revoke)
          throw error(403, "New access for everyone signed in can't be created. Grant named people or workflow identities instead.");
        if (subject === EVERYONE && !(a.org && perm === USE_HUB)) throw error(403, "Only organization admins may remove access for everyone signed in");
        if (!a.org && (perm === MANAGE_ACCESS || (revoke && perm === USE_HUB && gs.can(subject, hub, MANAGE_ACCESS))))
          throw error(403, "Only organization admins may change administrator access");
        if (revoke) gs.revoke(subject, hub, perm, a.subject);
        else {
          if (state.suspended.has(subject)) throw error(409, "Reactivate this user before granting access");
          if (state.unavailable.has(subject))
            throw error(409, "This account is no longer in the chat app. Access resumes if it reappears.");
          gs.grant(subject, hub, perm, a.subject);
        }
        return { ok: true, revision: state.revision };
      }
      case "/access/apply": {
        const subject = String(body.subject || "").trim().toLowerCase();
        const hub = String(body.hub || "").trim().toLowerCase();
        requireHub(a, hub);
        if (!state.hubs.find((h) => h.key === hub).authoritative) throw error(409, LEGACY_MSG);
        if (subject === EVERYONE) throw error(403, "Public access is changed with the public-access toggle");
        const ops = body.operations || [];
        const existing = new Set(state.grants.filter(([, h]) => h === hub).map(([s, , p]) => `${s}|${p}`));
        let grants = false;
        for (const op of ops) {
          const perm = String(op.permission || "").trim().toLowerCase();
          if (!a.org && (perm === MANAGE_ACCESS || (op.action === "revoke" && perm === USE_HUB && gs.can(subject, hub, MANAGE_ACCESS))))
            throw error(403, "Only organization admins may change administrator access");
          const entry = state.catalogs[hub].find((p) => p.permission === perm);
          if (!grantable(entry) && !(op.action === "revoke" && existing.has(`${subject}|${perm}`)))
            throw error(422, entry?.default === "included" ? `${entry.label} comes with Use this agent; it has no grant of its own.` : "Unknown permission for this hub");
          grants = grants || op.action === "grant";
        }
        if (grants) {
          if (state.suspended.has(subject)) throw error(409, "Reactivate this user before granting access");
          if (state.unavailable.has(subject)) throw error(409, "This account is no longer in the chat app. Access resumes if it reappears.");
        }
        if (body.expected_revision != null && body.expected_revision !== state.revision)
          throw error(409, "Access changed since you loaded it — someone else edited it. Reload and review the current access before saving.");
        for (const op of ops) {
          const perm = String(op.permission || "").trim().toLowerCase();
          if (op.action === "revoke") gs.revoke(subject, hub, perm, a.subject);
          else gs.grant(subject, hub, perm, a.subject);
        }
        return { ok: true, revision: state.revision };
      }
      case "/people": {
        const scopes = allowedHubs(a).map((h) => h.key);
        const visible = new Set(state.grants.filter(([, h]) => a.org || scopes.includes(h)).map(([s]) => s));
        const q = (params.q || "").toLowerCase();
        if (params.agent) requireHub(a, params.agent);
        const rows = [];
        for (const [subject, id] of Object.entries(state.identities).sort()) {
          if (!a.org && !visible.has(subject)) continue;
          if (!(subject + " " + (id.display || "")).toLowerCase().includes(q)) continue;
          const orgAdmin = gs.can(subject, ORG, MANAGE_ACCESS);
          const access = Object.fromEntries(scopes.map((h) => [h, gs.permissionsFor(subject, h)]));
          const isService = subject.startsWith("workflow:");
          if (params.status && identityStatus(subject) !== params.status) continue;
          if (params.role === "admin" && !orgAdmin) continue;
          if (params.role === "service" && !isService) continue;
          if (params.role === "regular" && (orgAdmin || isService)) continue;
          if (params.agent && !(access[params.agent] || []).length) continue;
          rows.push({
            subject,
            email: subject.includes("@") ? subject : null,
            display: id.display,
            owui_id: id.owui_id,
            pending: id.pending,
            status: identityStatus(subject),
            ...accountFlags(subject),
            organization_admin: orgAdmin,
            access,
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
        const before = accountFlags(subject);
        const changed = suspended || before.suspended;
        if (suspended) {
          state.grants = state.grants.filter(([s]) => s !== subject);
          state.suspended.add(subject);
          state.revision += 1;
        } else if (before.suspended) {
          state.suspended.delete(subject);
          state.revision += 1;
        }
        gs.audit(a.subject, suspended ? "suspend" : "reactivate", subject, ORG, null);
        const flags = accountFlags(subject);
        let message = null;
        if (!suspended && flags.account_unavailable)
          message =
            (changed ? "Admin block cleared. " : "Not blocked by an admin. ") +
            "This account is no longer in the chat app. Access resumes if it reappears.";
        return { ok: true, subject, changed, message, status: identityStatus(subject), ...flags };
      }
      case "/workflows": {
        const hubs = params.hub ? [params.hub] : allowedHubs(a).map((h) => h.key);
        if (params.hub) requireHub(a, params.hub);
        return { workflows: hubs.flatMap((h) => state.workflows[h].map((w) => decorate(w, state))) };
      }
      case "/runs": {
        // A named agent scopes to one bridge (per-step detail on run_id); no agent
        // → cross-agent over every agent this admin may manage. All filters run
        // before pagination; ordering is (started desc, id desc) for determinism.
        const scopes = params.hub ? [params.hub] : allowedHubs(a).map((h) => h.key);
        if (params.hub) requireHub(a, params.hub);
        const scopeSet = new Set(scopes);
        let runs = [];
        for (const [key, list] of Object.entries(state.runs)) {
          if (scopeSet.has(key.split(":")[0])) runs.push(...list);
        }
        if (params.workflow) runs = runs.filter((r) => r.name === params.workflow);
        if (params.run_id) runs = runs.filter((r) => r.id === params.run_id);
        const want = resolveStatuses(params.status); // throws 422 on an unknown filter
        if (want) runs = runs.filter((r) => want.includes(r.status));
        // Date window is on `created` (the ordering/pagination key), matching DBOS.
        if (params.since) runs = runs.filter((r) => r.created != null && r.created >= Date.parse(params.since));
        if (params.until) runs = runs.filter((r) => r.created != null && r.created <= Date.parse(params.until));
        runs = runs
          .slice()
          .sort((x, y) => (y.created || 0) - (x.created || 0) || (x.id < y.id ? 1 : x.id > y.id ? -1 : 0));
        if (params.run_id) return { runs, has_more: false };
        const offset = Number(params.offset || 0);
        const limit = Number(params.limit || 50);
        return {
          runs: runs.slice(offset, offset + limit).map((r) => ({ ...r, steps: undefined })),
          has_more: runs.length > offset + limit,
        };
      }
      case "/audit": {
        const hubs = params.hub ? [params.hub] : allowedHubs(a).map((h) => h.key);
        if (params.hub) requireHub(a, params.hub);
        let rows = hubs.flatMap((h) => state.decisions[h].map((d) => ({ ...d, hub: h })));
        if (params.user) rows = rows.filter((r) => r.user === params.user.toLowerCase());
        if (params.outcome === "allow" || params.outcome === "deny")
          rows = rows.filter((r) => r.decision === params.outcome);
        else if (params.denied === "true") rows = rows.filter((r) => r.decision === "deny");
        if (params.tool) rows = rows.filter((r) => r.tool === params.tool);
        if (params.surface) rows = rows.filter((r) => r.surface === params.surface);
        if (params.since) rows = rows.filter((r) => r.ts >= params.since);
        if (params.until) rows = rows.filter((r) => r.ts <= params.until);
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
        if (params.actor) rows = rows.filter((r) => r.actor === params.actor.toLowerCase());
        if (params.action) rows = rows.filter((r) => r.action === params.action);
        if (params.since) rows = rows.filter((r) => Number(r.ts) >= Number(params.since));
        if (params.until) rows = rows.filter((r) => Number(r.ts) <= Number(params.until));
        const offset = Number(params.offset || 0);
        const limit = Number(params.limit || 50);
        return { rows: rows.slice(offset, offset + limit) };
      }
      case "/sync":
        if (!a.org) throw error(403, "Organization admin required");
        state.visibility = { state: "ok", models: 2, updated: NOW };
        return state.visibility;
      case "/summary": {
        const periods = { "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400 };
        const period = params.period || "7d";
        if (!periods[period]) throw error(422, "period must be one of 24h, 7d, 30d");
        const scale = { "24h": 1, "7d": 6, "30d": 22 }[period];
        const usage = {
          finance: { chats: 12, messages: 48, active_users: 5, input_tokens: 182000, output_tokens: 24100, cost_usd: 1.84, unpriced: 0, last_activity: NOW - 2 * HOUR, denials: 2 },
          support: { chats: 30, messages: 95, active_users: 11, input_tokens: 410000, output_tokens: 61000, cost_usd: 3.9, unpriced: 3, last_activity: NOW - 300, denials: 0 },
          itops: { chats: 0, messages: 0, active_users: 0, input_tokens: 0, output_tokens: 0, cost_usd: null, unpriced: 0, last_activity: null, denials: 0 },
        };
        const hubs = allowedHubs(a).map((h) => {
          const u = usage[h.key];
          const flows = state.workflows[h.key] || [];
          const subjects = new Set(state.grants.filter(([s, hub]) => hub === h.key && !s.startsWith("workflow:")).map(([s]) => s));
          return {
            key: h.key, name: h.name, managed: h.authoritative,
            chats: u.chats * scale, messages: u.messages * scale, active_users: u.active_users,
            input_tokens: u.input_tokens * scale, output_tokens: u.output_tokens * scale,
            cost_usd: u.cost_usd == null ? null : Math.round(u.cost_usd * scale * 100) / 100,
            unpriced: u.unpriced * scale, last_activity: u.last_activity,
            users_with_access: h.authoritative ? [...subjects].filter((s) => s !== EVERYONE).length : null,
            everyone: h.authoritative ? subjects.has(EVERYONE) : null,
            denials: u.denials * scale, has_workflows: flows.length > 0,
            runs: flows.length ? 4 * scale : null, failed: flows.length ? (h.key === "finance" ? scale : 0) : null,
            missed: flows.length ? (h.key === "itops" ? 3 : 0) : null,
          };
        });
        const sum = (k) => hubs.reduce((n, h) => n + (h[k] || 0), 0);
        const costs = hubs.map((h) => h.cost_usd).filter((c) => c != null);
        const withWork = hubs.some((h) => h.has_workflows);
        return {
          period, since: NOW - periods[period], generated: NOW, recording_since: NOW - 20 * 86400,
          has_workflows: withWork, runs_available: true,
          totals: {
            chats: sum("chats"), messages: sum("messages"), active_users: sum("active_users"),
            input_tokens: sum("input_tokens"), output_tokens: sum("output_tokens"),
            cost_usd: costs.length ? costs.reduce((a2, b) => a2 + b, 0) : null,
            unpriced: sum("unpriced"), denials: sum("denials"),
            runs: withWork ? sum("runs") : null, failed: withWork ? sum("failed") : null,
            missed: withWork ? sum("missed") : null,
          },
          hubs,
          // Sign-in accounts in the viewer's scope; the same for every period.
          user_accounts: { accounts: a.org ? 42 : 9, hubs: hubs.length },
        };
      }
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

// Mirrors hubzoid/capabilities.py entries: the original four fields plus the
// Console's presentation fields. `extra` overrides the defaults.
function perm(permission, label, description, sensitive = false, extra = {}) {
  const group =
    permission === USE_HUB ? "hub" : permission === MANAGE_ACCESS ? "admin" : permission === "curator" ? "tools" : "restricted";
  return {
    permission, label, description, sensitive, group, surfaces: [], status: "", available: true,
    default: "grant", delegate_grantable: permission !== MANAGE_ACCESS, obsolete: false, ...extra,
  };
}
// A granted id that is no longer in the catalogue (capabilities._obsolete).
function obsoletePerm(permission) {
  return perm(permission, permission.replaceAll("_", " ").replace(/\b\w/g, (c) => c.toUpperCase()),
    "This capability no longer exists in this agent. You can remove it, but it can't be granted again.",
    false, { group: "obsolete", status: "No longer available", available: false, obsolete: true });
}
// Grantable: current and not included with Use this agent (service._grantable).
const grantable = (p) => !!p && !p.obsolete && p.default !== "included";
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
    created: started, // synthetic runs create and start together
    started,
    completed: started + durationMs,
    duration_ms: durationMs,
    output,
    error: err,
    steps: steps.map((s) => ({ ...s, started: started + s.started, completed: started + s.completed })),
  };
}
function running(hub, name, id, startedSeconds) {
  return {
    hub,
    id,
    name,
    status: "PENDING",
    created: startedSeconds * 1000,
    started: startedSeconds * 1000,
    completed: null,
    duration_ms: null,
    output: null,
    error: null,
    steps: [],
  };
}
function step(name, started, completed, output, err = null) {
  return { name, started, completed, output, error: err };
}
function error(status, detail, code, extra) {
  const e = new Error(detail);
  e.status = status;
  e.detail = detail;
  e.code = code;
  e.extra = extra;
  return e;
}
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const conflict = (detail) => error(409, detail);

module.exports = { createFixture, perm };
