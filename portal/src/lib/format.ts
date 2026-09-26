import type { AuditRow, Permission } from "../api";

// ---- time ------------------------------------------------------------------

/** Accepts epoch seconds, epoch milliseconds or an ISO string. */
export function parseTime(value: number | string | null | undefined): Date | null {
  if (value == null || value === "") return null;
  const date =
    typeof value === "number"
      ? new Date(value < 1e12 ? value * 1000 : value)
      : new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** An ISO string with no timezone marker (no Z, no ±hh:mm). Historical audit
 *  lines were written with the server's local clock and no offset; we must not
 *  silently reinterpret those as the viewer's local time. */
function isNaiveIso(value: number | string | null | undefined): value is string {
  return (
    typeof value === "string" &&
    /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(value) &&
    !/([zZ]|[+-]\d{2}:?\d{2})$/.test(value)
  );
}

export function formatTime(value: number | string | null | undefined) {
  // Naive timestamp: show the recorded wall clock as-is, labelled, never converted.
  if (isNaiveIso(value)) return `${value.replace("T", " ")} (server time)`;
  const date = parseTime(value);
  // Show the viewer's zone explicitly (e.g. "… PDT") so timestamps recorded in
  // UTC are never mistaken for local time.
  return date ? date.toLocaleString(undefined, { timeZoneName: "short" }) : "—";
}

export function relativeTime(
  value: number | string | null | undefined,
  now = Date.now(),
) {
  // No trustworthy instant for a naive timestamp — show the labelled wall clock
  // rather than a misleading "2 hours ago".
  if (isNaiveIso(value)) return formatTime(value);
  const date = parseTime(value);
  if (!date) return "—";
  const seconds = Math.round((date.getTime() - now) / 1000);
  const abs = Math.abs(seconds);
  const units: [number, Intl.RelativeTimeFormatUnit][] = [
    [60, "second"],
    [3600, "minute"],
    [86400, "hour"],
    [604800, "day"],
    [2629800, "week"],
    [31557600, "month"],
  ];
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  let divisor = 1;
  let unit: Intl.RelativeTimeFormatUnit = "year";
  for (const [limit, name] of units) {
    if (abs < limit) {
      unit = name;
      break;
    }
    divisor = limit;
  }
  if (unit === "year") divisor = 31557600;
  return rtf.format(Math.round(seconds / divisor), unit);
}

export function formatDuration(ms: number | null | undefined) {
  if (ms == null) return "—";
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  const minutes = Math.floor(ms / 60_000);
  const seconds = Math.round((ms % 60_000) / 1000);
  return `${minutes} min ${seconds} s`;
}

// ---- names -----------------------------------------------------------------

export const EVERYONE = "*";
export const ORG = "*";
export const USE_HUB = "use_hub";
export const MANAGE_ACCESS = "manage_access";

export const humanize = (s: string) =>
  s.replaceAll("-", " ").replaceAll("_", " ");

export const isService = (subject: string) => subject.startsWith("workflow:");

/** A person's readable name; falls back to the identity itself. */
export function personName(subject: string, display?: string | null) {
  if (subject === EVERYONE) return "Everyone signed in";
  if (display && display.trim() && display.trim() !== subject) return display;
  return subject;
}

export function initials(subject: string, display?: string | null) {
  if (subject === EVERYONE) return "All";
  if (isService(subject)) return "⚙";
  const name = personName(subject, display);
  const words = name.replace(/@.*/, "").split(/[\s._-]+/).filter(Boolean);
  return words
    .slice(0, 2)
    .map((w) => w[0]!.toUpperCase())
    .join("");
}

/** A subject may be an email or a `workflow:<function>` service identity. */
export function validateSubject(raw: string): string | null {
  const value = raw.trim().toLowerCase();
  if (!value) return "Enter an email address.";
  if (value === EVERYONE) return "Access for everyone signed in can’t be granted. Add people by name.";
  if (/^workflow:(?:md:)?[a-z0-9_.-]+$/.test(value)) return null;
  if (/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value)) return null;
  return "Enter a valid email address.";
}

export const normalizeSubject = (raw: string) => raw.trim().toLowerCase();

export type Catalog = Record<string, Permission>;

export function toCatalog(permissions: Permission[] | undefined): Catalog {
  const out: Catalog = {};
  for (const p of permissions ?? []) out[p.permission] = p;
  return out;
}

export function capabilityLabel(
  permission: string,
  catalog?: Catalog | null,
) {
  if (catalog?.[permission]) return catalog[permission].label;
  if (permission === USE_HUB) return "Use this agent";
  if (permission === MANAGE_ACCESS) return "Manage access";
  return humanize(permission).replace(/^\w/, (c) => c.toUpperCase());
}

// ---- capability groups -------------------------------------------------------

/** Drawer sections in display order. Presentation only: grants never change. */
export const CAPABILITY_GROUPS = [
  { key: "hub", title: "Hub access" },
  { key: "tools", title: "Hubzoid tools" },
  { key: "restricted", title: "Restricted tools" },
  { key: "workflows", title: "Workflows" },
  { key: "admin", title: "Administration" },
  { key: "obsolete", title: "No longer available" },
] as const;

/** The group a capability belongs to. An older catalogue has no groups, so
 *  the built-in ids are placed by name and everything else is restricted. */
export function capabilityGroup(p: Pick<Permission, "permission" | "group" | "obsolete">): string {
  if (p.obsolete) return "obsolete";
  if (p.group && CAPABILITY_GROUPS.some((g) => g.key === p.group)) return p.group;
  if (p.permission === USE_HUB) return "hub";
  if (p.permission === MANAGE_ACCESS) return "admin";
  if (p.permission === "curator") return "tools";
  return p.group ? "tools" : "restricted";
}

/**
 * The drawer's sections for one person: catalogue order within fixed groups,
 * empty groups left out. Obsolete grants appear only for someone who holds
 * them; a held id missing from the catalogue (an older bridge) counts too.
 */
export function groupCapabilities(permissions: Permission[], held: string[]) {
  const known = new Set(permissions.map((p) => p.permission));
  const rows: Permission[] = [
    ...permissions.filter((p) => !p.obsolete || held.includes(p.permission)),
    ...held
      .filter((p) => !known.has(p))
      .map((p) => ({
        permission: p,
        label: capabilityLabel(p),
        description: "This capability no longer exists in this agent. You can remove it, but it can’t be granted again.",
        sensitive: false,
        obsolete: true,
      })),
  ];
  return CAPABILITY_GROUPS.map((g) => ({
    ...g,
    items: rows.filter((p) => capabilityGroup(p) === g.key),
  })).filter((g) => g.items.length > 0);
}

/** Can be granted: current, and not included with Use this agent. */
export const isGrantable = (p?: Permission) => !!p && !p.obsolete && p.default !== "included";

const SURFACE_NAMES: Record<string, string> = {
  chat: "chat",
  mcp: "assistants over MCP",
  workflow: "workflows",
};

/** Help text for where a capability acts and whether it can run yet. */
export function capabilityNotes(p: Permission): string[] {
  const notes: string[] = [];
  const where = (p.surfaces ?? []).map((s) => SURFACE_NAMES[s] ?? s);
  if (where.length) notes.push(`Works in ${where.join(" and ")}.`);
  if (p.obsolete) return notes;
  if (p.available === false)
    notes.push(
      p.status === "Disabled for this hub"
        ? "Disabled for this agent, so it has no effect until an operator enables it."
        : p.default === "included"
          ? `${p.status || "Not configured"}: it can’t run until an operator adds its settings.`
          : `${p.status || "Not configured"}: it can be granted, but can’t run until an operator adds its settings.`,
    );
  else if (p.available === null)
    notes.push("Its settings may be in a secret this Console doesn’t read, so they weren’t checked.");
  return notes;
}

// ---- statuses --------------------------------------------------------------

type Presentation = { label: string; color: string; hint: string };

export function accountStatus(status: string): Presentation {
  switch (status) {
    case "active":
      return { label: "Active", color: "green", hint: "Signed up and approved in the chat app." };
    case "awaiting-signup":
      return {
        label: "Not signed up yet",
        color: "default",
        hint: "Access is ready; it applies once they sign up with this email.",
      };
    case "pending-approval":
      return {
        label: "Awaiting approval",
        color: "gold",
        hint: "Signed up, but an administrator has not approved the account yet.",
      };
    case "blocked":
      return { label: "Blocked", color: "red", hint: "All agent access is suspended." };
    case "service":
      return { label: "Legacy service identity", color: "default", hint: "Created before workflows ran as user accounts; its access is kept." };
    case "everyone":
      return { label: "Public", color: "purple", hint: "Applies to everyone who can sign in." };
    default:
      return { label: humanize(status), color: "default", hint: "" };
  }
}

export function personStatus(p: {
  subject: string;
  blocked: boolean;
  owui_id: string | null;
  pending: number | boolean;
}) {
  if (p.blocked) return "blocked";
  if (isService(p.subject)) return "service";
  if (!p.owui_id) return "awaiting-signup";
  if (p.pending) return "pending-approval";
  return "active";
}

export function workflowState(state: string): Presentation {
  switch (state) {
    case "definition-disabled": return { label: "Disabled in file", color: "default", hint: "Enable this task in its schedule file when it is ready." };
    case "scheduled":
      return { label: "Scheduled", color: "green", hint: "The scheduler is running and will fire this on time." };
    case "manual":
      return { label: "Manual", color: "default", hint: "No schedule; runs only when started explicitly." };
    case "event":
      return { label: "On webhook", color: "green", hint: "Runs when its webhook receives an event." };
    case "paused":
      return {
        label: "Paused",
        color: "orange",
        hint: "An operator paused this schedule. Resume it on the server with `hubzoid schedule resume`.",
      };
    case "disabled":
      return { label: "Schedules off", color: "default", hint: "Schedules are disabled for this deployment." };
    case "stale":
      return {
        label: "Scheduler stopped",
        color: "red",
        hint: "No dispatcher heartbeat recently; scheduled runs will not fire until it restarts.",
      };
    case "error":
      return { label: "Definition error", color: "red", hint: "The workflow could not be loaded." };
    default:
      return { label: humanize(state), color: "default", hint: "" };
  }
}

export function runStatus(status: string): Presentation {
  const s = status.toUpperCase();
  if (s === "SUCCESS") return { label: "Succeeded", color: "green", hint: "" };
  if (s === "ERROR") return { label: "Failed", color: "red", hint: "" };
  if (s === "PENDING") return { label: "Running", color: "processing", hint: "" };
  if (s === "ENQUEUED") return { label: "Queued", color: "blue", hint: "" };
  if (s === "CANCELLED") return { label: "Cancelled", color: "default", hint: "" };
  if (s.includes("RETRIES") || s.includes("RECOVERY"))
    return { label: "Gave up", color: "red", hint: humanize(status.toLowerCase()) };
  return { label: humanize(status.toLowerCase()), color: "default", hint: "" };
}

// ---- activity sentences -------------------------------------------------------

export type Sentence = {
  /** Plain segments and highlighted entities, in reading order. */
  parts: { text: string; kind?: "person" | "capability" | "agent" | "actor" | "tool" }[];
  detail?: string;
  tone: "neutral" | "positive" | "negative";
};

const text = (t: string) => ({ text: t });
const person = (t: string) => ({ text: t, kind: "person" as const });
const actor = (t: string) => ({ text: t, kind: "actor" as const });
const capability = (t: string) => ({ text: t, kind: "capability" as const });
const agent = (t: string) => ({ text: t, kind: "agent" as const });
const tool = (t: string) => ({ text: t, kind: "tool" as const });

export type ActivityContext = {
  agentName: (key: string) => string;
  catalog: (key: string) => Catalog | undefined;
  people: (subject: string) => string;
};

const inAgent = (hubName: string) => (hubName ? [text(" in "), agent(hubName)] : []);

/** `md:<task>` is a markdown schedule task; anything else is a code workflow. */
function workflowLabel(name?: string | null) {
  if (!name) return "a workflow";
  return name.startsWith("md:") ? `the ${name.slice(3)} schedule` : `the ${name} workflow`;
}

export function describeAccessChange(row: AuditRow, ctx: ActivityContext): Sentence {
  const who =
    row.actor === "owui-identity"
      ? "Account sync"
      : row.actor === "bootstrap"
        ? "Initial setup"
        : row.actor?.startsWith("cli:")
          ? `Server operator (${row.actor.slice(4)})`
          : row.actor
          ? ctx.people(row.actor)
          : "System";
  const subjectName = row.subject ? ctx.people(row.subject) : "";
  const hubName = row.hub && row.hub !== ORG ? ctx.agentName(row.hub) : "";
  const cap = row.permission
    ? capabilityLabel(row.permission, row.hub ? ctx.catalog(row.hub) : undefined)
    : "";
  const isOrgAdmin = row.hub === ORG && row.permission === MANAGE_ACCESS;
  switch (row.action) {
    case "grant":
      if (isOrgAdmin)
        return { tone: "positive", parts: [actor(who), text(" made "), person(subjectName), text(" an organization administrator")] };
      return {
        tone: "positive",
        parts: [actor(who), text(" allowed "), person(subjectName), text(" to "), capability(cap), text(" in "), agent(hubName)],
      };
    case "revoke":
      if (isOrgAdmin)
        return { tone: "negative", parts: [actor(who), text(" removed organization administrator rights from "), person(subjectName)] };
      if (row.permission === USE_HUB)
        return {
          tone: "negative",
          parts: [actor(who), text(" removed "), person(subjectName), text("’s access to "), agent(hubName)],
          detail: "Removing agent access also removes every capability in that agent.",
        };
      return {
        tone: "negative",
        parts: [actor(who), text(" removed "), capability(cap), text(" from "), person(subjectName), text(" in "), agent(hubName)],
      };
    case "revoke_all":
      return { tone: "negative", parts: [actor(who), text(" removed all access from "), person(subjectName)] };
    case "suspend":
      return { tone: "negative", parts: [actor(who), text(" blocked "), person(subjectName)] };
    case "reactivate":
      return { tone: "positive", parts: [actor(who), text(" reactivated "), person(subjectName)] };
    // Note: the store never writes action "bootstrap" — "bootstrap" is only ever
    // the actor. The first-admin event arrives as a "grant" of manage_access in
    // the org domain and renders through the isOrgAdmin branch above
    // ("Initial setup made … an organization administrator").
    case "activate":
      return {
        tone: "neutral",
        parts: hubName
          ? [text("Access for "), agent(hubName), text(" is now managed in this portal")]
          : [text("Access management was activated for this deployment")],
      };
    case "replace_hub_grants":
      return { tone: "neutral", parts: [actor(who), text(" replaced all access for "), agent(hubName), text(" (migration)")] };
    case "rollback":
      return { tone: "neutral", parts: [actor(who), text(" rolled back access for "), agent(hubName), text(" to a backup")] };
    case "restore_grant":
      return {
        tone: "neutral",
        parts: [actor(who), text(" restored "), capability(cap), text(" for "), person(subjectName), text(" in "), agent(hubName), text(" from a backup")],
      };
    case "account_replaced":
      return {
        tone: "negative",
        parts: [text("A new chat account reused "), person(subjectName), text("’s email")],
        detail: "Previous access was removed and the identity blocked until an administrator reviews it.",
      };
    // Run controls come from `hubzoid schedule pause | resume | cancel` on the
    // server; the target is kept in the permission column.
    case "workflow_pause":
      return { tone: "negative", parts: [actor(who), text(" paused "), text(workflowLabel(row.permission)), text(" in "), agent(hubName)] };
    case "workflow_resume":
      return { tone: "positive", parts: [actor(who), text(" resumed "), text(workflowLabel(row.permission)), text(" in "), agent(hubName)] };
    case "run_cancel":
      return { tone: "negative", parts: [actor(who), text(" cancelled run "), text(row.permission || ""), text(" in "), agent(hubName)] };
    case "account_unavailable":
      return {
        tone: "negative",
        parts: [person(subjectName), text("’s chat account is no longer available")],
        detail: "Access is paused until the account reappears in the chat app.",
      };
    // Account actions (the Console's account directory). The subject is the email.
    case "account_create":
      return { tone: "positive", parts: [actor(who), text(" created a chat account for "), person(subjectName)] };
    case "account_create_failed":
      return {
        tone: "negative",
        parts: [actor(who), text(" couldn’t create a chat account for "), person(subjectName)],
        detail: "No access was granted.",
      };
    case "account_approve":
      return { tone: "positive", parts: [actor(who), text(" approved "), person(subjectName), text("’s chat account")] };
    case "account_password_reset":
      return {
        tone: "neutral",
        parts: [actor(who), text(" reset the password for "), person(subjectName)],
        detail: "Their existing chat sessions were signed out.",
      };
    case "account_role":
      // The new chat-app role is kept in the permission column.
      return {
        tone: "neutral",
        parts: [actor(who), text(" changed "), person(subjectName), text(`’s chat-app role to ${row.permission === "admin" ? "admin" : "user"}`)],
      };
    case "account_delete":
      return {
        tone: "negative",
        parts: [actor(who), text(" deleted "), person(subjectName), text("’s chat account")],
        detail: "Their access was removed first.",
      };
    // Changes proposed from chat, WhatsApp or MCP and confirmed in the Console.
    case "change_proposed":
      return {
        tone: "neutral",
        parts: [actor(who), text(" proposed a change for "), person(subjectName), ...inAgent(hubName)],
        detail: "Nothing changes until it is confirmed in the Console.",
      };
    case "change_confirmed":
      return { tone: "positive", parts: [actor(who), text(" confirmed a change for "), person(subjectName), ...inAgent(hubName)] };
    case "change_rejected":
      return { tone: "neutral", parts: [actor(who), text(" rejected a proposed change for "), person(subjectName), ...inAgent(hubName)] };
    case "change_expired":
      return {
        tone: "neutral",
        parts: [text("A proposed change for "), person(subjectName), ...inAgent(hubName), text(" expired")],
        detail: "It was not confirmed in time, so nothing changed.",
      };
    case "change_failed":
      return {
        tone: "negative",
        parts: [actor(who), text(" confirmed a change for "), person(subjectName), ...inAgent(hubName), text(", but it failed")],
        detail: "Nothing was applied.",
      };
    default:
      return {
        tone: "neutral",
        parts: [
          actor(who),
          text(` ${humanize(row.action || "changed")} `),
          ...(subjectName ? [person(subjectName)] : []),
          ...(cap ? [text(" "), capability(cap)] : []),
          ...(hubName ? [text(" in "), agent(hubName)] : []),
        ],
      };
  }
}

export function explainDecisionReason(reason: string | undefined) {
  switch (reason) {
    case "grant":
    case "group":
      return "";
    case "unrestricted":
      return "This tool does not require a permission.";
    case "no-grant":
      return "They do not have the permission this tool requires.";
    case "no-group":
      return "They are not in the required group (legacy access for an unmigrated agent).";
    case "anonymous":
      return "The caller was not signed in.";
    case "blocked":
      return "Their account is blocked.";
    case "store-error":
      return "The access store was unavailable, so the request was refused.";
    default:
      if (reason?.startsWith("surface:"))
        return `Restricted tools are not allowed from ${humanize(reason.slice(8))}.`;
      return reason ? humanize(reason) : "";
  }
}

export function describeDecision(row: AuditRow, ctx: ActivityContext): Sentence {
  const who = row.user && row.user !== "anonymous" ? ctx.people(row.user) : "Someone not signed in";
  const hubName = row.hub ? ctx.agentName(row.hub) : "";
  const toolName = row.tool || "a tool";
  const surface = row.surface && row.surface !== "system" ? ` via ${humanize(row.surface)}` : "";
  const detail = explainDecisionReason(row.reason);
  if (row.decision === "deny")
    return {
      tone: "negative",
      parts: [person(who), text(" was denied "), tool(toolName), text(" in "), agent(hubName), text(surface)],
      detail,
    };
  return {
    tone: "positive",
    parts: [person(who), text(" used "), tool(toolName), text(" in "), agent(hubName), text(surface)],
    detail,
  };
}

/** Keep unfamiliar cron expressions exact rather than inventing a schedule. */
export function describeCron(cron: string): string {
  const p = cron.trim().split(/\s+/);
  if (p.length !== 5) return "Custom schedule";
  const [minute, hour, day, month, weekday] = p;
  if (day !== "*" || month !== "*") return "Custom schedule";
  if (hour === "*" && weekday === "*" && minute === "*") return "Every minute";
  if (hour === "*" && weekday === "*" && /^\*\/\d+$/.test(minute)) return `Every ${minute.slice(2)} minutes`;
  if (/^\d+$/.test(minute) && /^\d+$/.test(hour)) {
    const time = `${hour.padStart(2, "0")}:${minute.padStart(2, "0")}`;
    if (weekday === "*") return `Daily at ${time}`;
    if (weekday === "1-5") return `Weekdays at ${time}`;
    if (/^[0-7]$/.test(weekday)) return `Every ${["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"][Number(weekday) % 7]} at ${time}`;
  }
  return "Custom schedule";
}

export function prettyOutput(value?: string | null): string {
  if (!value) return "";
  try { return JSON.stringify(JSON.parse(value), null, 2); } catch { return value; }
}

export const short = (n: number) => new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 1 }).format(n);
