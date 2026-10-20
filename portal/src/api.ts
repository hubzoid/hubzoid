export type Me = {
  subject: string;
  org_admin: boolean;
  manageable: string[];
  // What this viewer may grant in each agent they manage. A delegate's list is
  // their own current access there, minus Manage access. The server checks
  // every write again; this only shapes the UI.
  grantable?: Record<string, string[]>;
  account_admin?: boolean;
  can_create_accounts?: boolean;
  accounts_configured?: boolean;
  /** How a new account can sign in. Google appears only when the chat app
   *  attaches a Google sign-in to an existing account by email. */
  sign_in?: SignInOptions;
  via?: "session" | "api-key";
  /** Always true since 2.0 (the web app owns sign-in and connections). */
  web_app?: boolean;
};
export type SignInOptions = {
  password: boolean;
  google: boolean;
  /** Only these email domains may sign in with Google (absent: any). */
  google_domains?: string[];
};
export type SignIn = "password" | "google" | "directory";
/** What the hub folder holds, counted without loading it (GET /hubs). */
export type HubContents = { knowledge: number; skills: number; subagents: number; tool_files: number; connectors: number };
export type Hub = { key: string; name: string; path?: string; model_id?: string; can_chat?: boolean; contents?: HubContents };
export type Permission = {
  permission: string;
  label: string;
  description: string;
  sensitive: boolean;
  // Presentation from hubzoid/capabilities.py. Optional so an older bridge's
  // catalogue still renders (grouped by id instead).
  /** hub | tools | restricted | workflows | admin | obsolete */
  group?: string;
  /** Optional sub-heading inside the group: "" | workflows | access. The
   *  server lists unsectioned rows first; an unknown key renders unsectioned. */
  section?: string;
  /** Where it acts, implemented surfaces only: chat, mcp, workflow. */
  surfaces?: string[];
  /** Short configuration status, e.g. "Jev key missing"; empty when ready. */
  status?: string;
  /** true: settings present · false: missing or disabled · null: not checked. */
  available?: boolean | null;
  /** "included" comes with Use this agent and has no grant of its own. */
  default?: "grant" | "included";
  delegate_grantable?: boolean;
  /** A granted id that no longer exists: removable, never grantable. */
  obsolete?: boolean;
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
  can_manage_admins: boolean;
  permissions: Permission[];
  rows: AccessRow[];
  total: number;
  // An existing "everyone signed in" grant (carried over; can't be created).
  public: boolean;
  /** Chat accounts that enter only through it (shown before removal). */
  public_reliant?: number;
  // Policy revision at load time — sent back with the first save so the backend
  // can reject an edit built on access another admin has since changed.
  revision: number;
  // Capabilities this viewer may grant or remove here (display only).
  grantable?: string[];
  viewer?: string;
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
  // Who a run of this workflow acts as, resolved the way a run resolves it.
  runs_as?: { account: string | null; source: string | null; via: string | null; error: string | null };
  // The webhook that starts this workflow, when it runs on events.
  webhook?: string | null;
  // Workflows from chat: offered as a "Run for me" tool, schedulable by people.
  schedule_text?: string;
  as_tool?: boolean;
  schedulable?: boolean;
  sensitive?: boolean;
  has_inputs?: boolean;
  title?: string;
  input_schema?: InputSchema | null;
};
export type InputProperty = {
  type?: string; enum?: unknown[]; default?: unknown; description?: string;
  anyOf?: InputProperty[]; hidden?: boolean; $ref?: string;
};
export type InputSchema = {
  type: "object";
  properties: Record<string, InputProperty>;
  required?: string[];
  $defs?: Record<string, InputProperty>;
};
/** A schedule a person saved from chat. It runs as them. */
export type PersonalSchedule = {
  id: string;
  owner: string;
  name: string;
  workflow: string;
  report: string;
  inputs: string;
  when: string;
  timezone: string;
  state: "testing" | "active" | "paused" | "stopped";
  reason: string | null;
  state_label: string;
  next_fire: number | null;
  last_status: string | null;
  last_at: number | null;
  last_label: string;
  last_run?: string | null;
  recovery?: string | null;
  cost_30d: number;
};
/** The signed-in person's own schedules and reports, on every agent (`GET /my`). */
export type ScheduleLink = { title: string; url: string };
export type MySchedule = Omit<PersonalSchedule, "owner" | "cost_30d"> & { hub: string; reports: ScheduleLink[] };
export type MyReport = {
  id: string;
  hub: string;
  title: string;
  report: string;
  url: string;
  updated: number | null;
  state: "live" | "stopped" | "failed";
  company_on: boolean;
};
/** `api`: where this agent takes actions (its own bridge in a gateway). */
export type MyAgent = { key: string; name: string; api: string; chat: string | null };
export type MyData = {
  subject: string;
  admin: boolean;
  /** Blocked by an administrator: no agents, no connections. */
  blocked: boolean;
  web_app: boolean;
  agents: MyAgent[];
  schedules: MySchedule[];
  reports: MyReport[];
  catalog?: { hub: string; workflow: string; title: string; schedulable: boolean; schema: InputSchema | null }[];
  /** Whether there are connections to see (one to connect, or one connected). */
  connections: boolean;
};
/** One run of a schedule: status and times only. */
export type ScheduleRun = { id?: string; status: string; started: number | null; completed: number | null; duration_ms: number | null };
/** One live report link: a team report or a person's own. */
export type Report = {
  id: string;
  title: string;
  key: string;
  workflow: string;
  report: string;
  scope: "team" | "personal";
  owner: string | null;
  updated: number | null;
  state: "live" | "stopped" | "failed";
  next: number | null;
  audience: string;
  company_on: boolean;
};
// A webhook this agent declares: where it receives events and how they went.
// Payloads, headers and digests never leave the server.
export type Webhook = {
  name: string;
  url: string;
  verify: string;
  workflows: string[];
  last_24h: { accepted: number; running: number; succeeded: number; failed: number };
  failures: {
    id: string;
    workflow: string;
    created: number;
    updated: number;
    attempt: number;
    error: string | null;
    redrive: string;
  }[];
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
  // The account the run acted as (from its identity step), if recorded.
  run_as?: string | null;
  // True when the run has a result that only its own account may see.
  redacted?: boolean;
  // How it started: schedule, personal_schedule, tool, operator, cli, webhook, update.
  trigger?: string | null;
  started_by?: string | null;
  inputs?: string | null;
  scope?: string | null;
  steps?: {
    name: string;
    started: number | null;
    completed: number | null;
    output: string | null;
    error: string | null;
    redacted?: boolean;
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
  /** Signed in to read a shared report; no agent access unless granted. */
  reader?: boolean;
  access: Record<string, string[]>;
  /** The number their WhatsApp and Telegram messages come from (digits).
   *  Shown to organization administrators only. */
  phone?: string | null;
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
export type Overview = {
  hubs: number;
  people: number;
  grants: number;
};

export type SummaryHub = {
  key: string;
  name: string;
  chats: number;
  messages: number;
  active_users: number;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number | null;
  unpriced: number;
  last_activity: number | null;
  users_with_access: number;
  everyone: boolean;
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
  /** Sign-in accounts in the viewer's scope, independent of `period`.
   *  `accounts` is null when the account directory could not be read. */
  user_accounts?: { accounts: number | null; hubs: number };
};

// status 0 means the request never got a response (network/abort): the server
// may or may not have applied it. `certain` is true only when we have a response
// that tells us the outcome definitively — a 4xx client rejection means nothing
// was committed; a 5xx or no-response is uncertain (an atomic write may have
// committed just before the connection dropped).
export class ApiError extends Error {
  status: number;
  certain: boolean;
  /** Stable reason from the access service, e.g. "account_exists". */
  code?: string;
  /** Structured detail sent with the refusal, e.g. the account a partial
   *  create made ({ account: { subject, name } }). Never a secret. */
  data: Record<string, unknown>;
  constructor(message: string, status: number, code?: string, data?: Record<string, unknown>) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.certain = status >= 400 && status < 500;
    this.code = code;
    this.data = data ?? {};
  }
}

/** Signed out, or the session expired: set by a 401 and cleared only by a
 *  successful answer, so another request's 403 or network error can't hide
 *  it. Error views then offer Sign in. */
export let signedOut = false;
function noteSession(status: number) {
  if (status === 401) signedOut = true;
  else if (status >= 200 && status < 300) signedOut = false;
}

/** The sign-in page, coming back to this Console page afterwards. */
export const signInHref = () =>
  `/auth?redirect=${encodeURIComponent(location.pathname + location.hash)}`;

export type ChangeRequest = {
  id: string;
  status: "pending" | "applying" | "confirmed" | "rejected" | "expired" | "failed";
  kind: "access" | "account";
  hub: string;
  hub_name: string;
  target: string;
  surface: string | null;
  created: number;
  expires: number;
  decided: number | null;
  plan:
    | { kind: "access"; hub: string; subject: string; grant: string[]; revoke: string[] }
    | { kind: "account"; hub: string; email: string; name: string; grant: string[] };
  plan_hash: string;
  summary: string;
  result: string | null;
  labels: Record<string, Permission>;
  current?: string[];
  problem: string | null;
};

export type AccountCreated = {
  ok: boolean;
  subject: string;
  name: string;
  grants: Record<string, string[]>;
  sign_in?: SignIn;
};


/** Access given to an existing account (POST /accounts/grant). */
export type AccountGranted = {
  ok: boolean;
  subject: string;
  name: string | null;
  grants: Record<string, string[]>;
};

export async function request<T>(
  path: string,
  body?: unknown,
  signal?: AbortSignal,
  method?: "GET" | "POST" | "PUT" | "DELETE",
  /** Another bridge's API in a gateway (an agent's `api`); this one by default. */
  base = "/portal/api",
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(base + path, {
      credentials: "include",
      signal,
      method: method ?? (body === undefined ? "GET" : "POST"),
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
  noteSession(response.status);
  if (!response.ok) {
    let message = `${response.status} — Request failed`;
    let code: string | undefined;
    let data: Record<string, unknown> | undefined;
    try {
      data = await response.json();
      message = typeof data?.detail === "string" ? data.detail : message;
      code = typeof data?.code === "string" ? data.code : undefined;
    } catch {
      /* use status */
    }
    throw new ApiError(message, response.status, code, data && typeof data === "object" ? data : undefined);
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

// ---- Personal connections: the connector registry (organization administrators) ----

export type Connector = {
  id: string;
  name: string;
  url: string;
  auth_type: "oauth" | "none" | "shared";
  client_id: string | null;
  /** A pre-registered client secret is stored. The secret itself is never sent. */
  has_client_secret: boolean;
  /** Shared key: the header the key is sent in, and whether a key is stored. */
  shared_header: string | null;
  has_shared_secret: boolean;
  scopes: string | null;
  /** Tool names people may use; null allows every tool. */
  tool_allowlist: string[] | null;
  enabled: boolean;
  /** The capability that gates it on managed agents: connector_<id>. */
  capability: string;
  /** Hubzoid registered its own client with the provider (RFC 7591). */
  dynamic_client: boolean;
  created_by: string | null;
  created_at: number;
  updated_at: number;
  /** Where the provider sends people back. Register it when pre-registering a client. */
  redirect_uri: string;
  /** How many people are connected. */
  connections: number;
  /** The agents that offer it (hub keys). */
  agents: string[];
};

/** A server from the hub folder's connectors/.mcp.json (read-only). Never a value. */
export type HubMcpServer = {
  name: string;
  kind: "command" | "url";
  env: { name: string; set: boolean }[];
  /** "file", or "hubzoid" for the shared browser Hubzoid adds. */
  source: "file" | "hubzoid";
};

export type ConnectorInput = {
  id?: string;
  name?: string;
  url?: string;
  auth_type?: "oauth" | "none" | "shared";
  client_id?: string | null;
  /** Omit to keep the stored secret; null removes it. */
  client_secret?: string | null;
  shared_header?: string | null;
  /** Omit to keep the stored key. */
  shared_secret?: string;
  scopes?: string | null;
  tool_allowlist?: string[] | null;
  enabled?: boolean;
};

/** POST /connectors/{id}/test: what discovery found. Changes nothing. */
export type ConnectorTest = {
  connector_id: string;
  auth_type: "oauth" | "none" | "shared";
  ok: boolean;
  redirect_uri: string;
  error?: { code: string; message: string };
  requires_auth?: boolean | null;
  status?: number | null;
  resource?: string | null;
  resource_metadata_url?: string | null;
  issuer?: string;
  authorization_endpoint?: string;
  token_endpoint?: string;
  registration_endpoint?: string | null;
  revocation_endpoint?: string | null;
  iss_parameter_supported?: boolean;
  pkce?: "S256" | "assumed";
  scopes_supported?: string[] | null;
  default_scope?: string | null;
  scope?: string | null;
  registration?: "pre-registered" | "dynamic" | "unavailable";
  token_endpoint_auth_methods?: string[] | null;
  notes?: string[];
  /** What the server lists, when the test could reach it with a credential. */
  tools?: ConnectorTool[] | null;
  tools_note?: string;
};

export type ConnectorTool = { name: string; description?: string | null };

/** Calls under /portal/api/connectors. Their errors are {"detail": {"code", "message"}}. */
export async function connectorsRequest<T>(
  path: string,
  method: "GET" | "POST" | "PATCH" | "PUT" | "DELETE" = "GET",
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  let response: Response;
  try {
    response = await fetch("/portal/api/connectors" + path, {
      credentials: "include",
      signal,
      method,
      headers: body === undefined ? undefined : { "content-type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (e) {
    throw new ApiError(e instanceof Error && e.message ? e.message : "Network error", 0);
  }
  noteSession(response.status);
  if (!response.ok) {
    let message = `${response.status}: Request failed`;
    let code: string | undefined;
    try {
      const data = await response.json();
      const detail = data?.detail;
      if (detail && typeof detail === "object") {
        if (typeof detail.message === "string") message = detail.message;
        if (typeof detail.code === "string") code = detail.code;
      } else if (typeof detail === "string") message = detail;
    } catch {
      /* use status */
    }
    throw new ApiError(message, response.status, code);
  }
  return (response.status === 204 ? undefined : await response.json()) as T;
}

// ---- Secrets (hubzoid.secretstore): write-only, never a value ----------------

export type SecretLevel = "deployment" | "hub" | "restricted";
export type SecretStored = {
  name: string;
  scope: "deployment" | "hub";
  revision: number;
  restricted: boolean;
  updated_by: string;
  updated_at: string;
  /** Bound to a destination (an Azure endpoint); a changed one asks for it again. */
  bound: boolean;
};
export type SecretRow = {
  name: string;
  level: SecretLevel;
  status: "set" | "missing" | "blank" | "unreadable" | "rebind";
  /** Where the value comes from: "Stored", "Hub .env", "Deployment environment"... */
  source: string | null;
  /** An operator source sets it: the Console cannot change it. */
  locked: boolean;
  lock_reason: string | null;
  stored: SecretStored | null;
  stored_status: "in_use" | "overridden" | "unreadable" | "rebind" | null;
  /** Why the agent expects it: model, webhook or declared (.env.example). */
  expected: string | null;
  /** Saved or removed since this agent started. */
  restart_needed: boolean;
};
export type SecretList = {
  hub: string;
  secrets: SecretRow[];
  /** Restricted secrets an agent administrator may not see by name. */
  restricted_hidden: number;
  can_manage: boolean;
};

/** The hub-scoped APIs (/api/hubs/{hub}/...: settings, secrets, channels) and /api/secrets. Errors are {"detail": {"code", "message", ...}}. */
export async function secretsRequest<T>(
  url: string,
  method: "GET" | "POST" | "PUT" | "DELETE" = "GET",
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(url, {
      credentials: "include",
      signal,
      method,
      headers: body === undefined ? undefined : { "content-type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (e) {
    throw new ApiError(e instanceof Error && e.message ? e.message : "Network error", 0);
  }
  noteSession(response.status);
  if (!response.ok) {
    let message = `${response.status}: Request failed`;
    let code: string | undefined;
    let data: Record<string, unknown> | undefined;
    try {
      const parsed = await response.json();
      const detail = parsed?.detail;
      if (detail && typeof detail === "object") {
        data = detail;
        if (typeof detail.message === "string") message = detail.message;
        if (typeof detail.code === "string") code = detail.code;
      } else if (typeof detail === "string") message = detail;
    } catch {
      /* use status */
    }
    throw new ApiError(message, response.status, code, data);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}
export const hubRequest = secretsRequest;

// ---- Hub settings and checks (hubzoid.hubs): settings.yaml, hub.settings.yaml ----

export type SettingProblem = { key?: string; code: string; message: string; file?: string; line?: number };
export type HubSetting = {
  name: string;
  type: "string" | "integer" | "number" | "boolean" | "string_list";
  required: boolean;
  secret: boolean;
  description: string;
  example: unknown;
  enum: (string | number)[] | null;
  /** environment | hub.settings.yaml | default | null */
  source: string | null;
  status: "set" | "missing" | "invalid";
  problems: SettingProblem[];
  /** Non-secret settings only: the effective value, and what hub.settings.yaml holds. */
  value?: unknown;
  stored?: unknown;
  default?: unknown;
  env?: string | null;
  /** Secret settings only: the reference in use and whether it resolves. */
  ref?: string;
  available?: boolean;
};
export type HubSettings = {
  revision: number;
  settings: HubSetting[];
  problems: SettingProblem[];
  can_manage: boolean;
  can_manage_secrets: boolean;
};
export type HubCheck = {
  id: string;
  status: "ok" | "info" | "warn" | "fail" | "pending";
  summary: string;
  detail: unknown;
};

// ---- One-time sign-in links (Hubzoid accounts, the default mode) -------------
// Add user and Reset password return a link for the person to set their own
// password instead of a password typed here.

export type LinkSignInOptions = SignInOptions & { links?: boolean };
/** A one-time sign-in link: absolute, or a path on this site. Works once. */
export type SignInLink = {
  link?: string | null;
  /** Unix seconds. */
  expires_at?: number;
  /** Set when the account exists but its link could not be made. */
  link_error?: string;
};
export type AccountCreatedWithLink = AccountCreated & SignInLink;

/** Whether this deployment hands out one-time sign-in links. */
export function usesSignInLinks(me?: Me | null): boolean {
  return !!(me?.sign_in as LinkSignInOptions | undefined)?.links;
}

/** The address to share: as given when absolute, else on this site. */
export function shareableLink(link: string): string {
  return /^https?:\/\//i.test(link) ? link : window.location.origin + link;
}

// ---- evals (portal_evals.py): a hub's cases, results and Console runs --------

/** A case's most recent recorded result: the newest results file that ran it. */
export type EvalLatest = {
  stamp: string;
  passed: boolean;
  reason: string;
  finished: string | null;
  /** console | schedule | cli | ci; null in files written before triggers. */
  trigger: string | null;
};

export type EvalCaseRow = {
  name: string;
  tags: string[];
  /** 5-field cron, or null for a case run only on demand. */
  schedule: string | null;
  /** Has `## Criteria`: graded by the judge. */
  judged: boolean;
  /** Prompts in the case: 1, or the number of turns of a conversation. */
  turns: number;
  /** The account the case runs as (its file), or null for the default. */
  run_as: string | null;
  enabled: boolean;
  /** What it checks, in a few words each. */
  checks: string[];
  /** The (first) prompt, shortened. */
  prompt: string;
  latest: EvalLatest | null;
  /** Its results in the last 10 runs, newest first. */
  history?: boolean[];
};

/** One run as counts. */
export type EvalScore = {
  stamp: string;
  passed: number;
  total: number;
  finished: string | null;
  trigger: string | null;
};

/** An eval run on the hub's workflow engine, scheduled or from the Console. */
export type EvalRunState = {
  id: string;
  source: "console" | "schedule";
  /** DBOS status: ENQUEUED, PENDING, SUCCESS, ERROR, CANCELLED, … */
  status: string;
  created: number | null;
  started: number | null;
  completed: number | null;
  /** The cases chosen; null means every enabled case. */
  cases: string[] | null;
  judge: boolean | null;
  requested_by: string | null;
  /** The results file the run wrote, once finished. */
  stamp: string | null;
  error: string | null;
};

export type EvalsOverview = {
  hub: string;
  /** The agent has an evals folder. */
  folder: boolean;
  docs: string;
  cases: EvalCaseRow[];
  /** Case files that could not be read. */
  errors: { file: string; error: string }[];
  /** How many results files are recorded. */
  runs: number;
  /** The latest run, the last 10 runs (oldest first), failing and never run cases. */
  score?: { latest: EvalScore | null; trend: EvalScore[]; failing: string[]; never_run: string[] };
  active: EvalRunState | null;
  last: EvalRunState | null;
  state_error?: string;
};

export type EvalRunSummary = {
  stamp: string;
  schema: number;
  trigger: string | null;
  started: string | null;
  finished: string | null;
  model: string | null;
  judge_model: string | null;
  judged: boolean;
  run_as: string | null;
  passed: number;
  failed: number;
  total: number;
};

export type EvalToolCall = {
  name: string;
  /** The call's arguments as recorded (secret-looking values redacted); null in older files. */
  args: unknown;
  ok: boolean | null;
  error: string | null;
  duration_ms: number | null;
  preview: string | null;
  /** 1-based turn of a conversation. */
  turn: number | null;
};

export type EvalCaseResult = {
  private?: boolean;
  name: string;
  tags: string[];
  passed: boolean;
  reason: string;
  /** Seconds. */
  duration: number;
  error: string | null;
  checks: { kind: string; passed: boolean; detail: string }[];
  judge: {
    score: number;
    threshold: number;
    reasoning: string;
    model: string | null;
    error: string | null;
    passed: boolean;
  } | null;
  /** The final reply. */
  answer: string;
  /** The prompt in the case file now (it may have changed since the run). */
  prompt: string | null;
  turns: { prompt: string; response: string }[] | null;
  tools: EvalToolCall[];
  run_as: string | null;
};

export type EvalRunDetail = EvalRunSummary & { hub: string; cases: EvalCaseResult[] };

export type EvalRunStarted = {
  ok: boolean;
  run_id: string;
  status: string;
  cases: string[];
  judge: boolean;
};

export type PersonalRun = {
  id: string; report: string; status: string; started: number | null;
  links: ScheduleLink[]; answer: string | null; recovery: string | null;
};
export type ScheduleEdit = { inputs: Record<string, unknown>; cadence: string; timezone: string; revision: number; schema: InputSchema | null };
