# Administering a multi-hub deployment

Hubzoid has two audiences: builders configure agents, tools and workflows;
end users sign in and use the agents they have been granted. In the default
mode, Hubzoid owns accounts, authentication and conversations. Open WebUI mode
uses Open WebUI's accounts and chat. Both use Hubzoid's agent/tool permissions
and execution inspection. Open **Admin Console** from the web app's account
menu, or visit `/portal/`. In Open WebUI mode it appears above the profile in
the sidebar for administrators and people with Manage access.

## One deployment, several agents

```bash
export HUBZOID_AUTH=true
export HUBZOID_ADMIN_EMAIL=operator@example.com
export HUBZOID_ADMIN_PASSWORD='your-strong-bootstrap-password'
hubzoid gateway ./finance ./operations --data-dir ./gateway-data
```

On a fresh default-mode deployment this creates the first administrator.
Sign in with that account, then add people in the Console. It gives one-time
sign-in links and sends no email. See [authentication](auth.md).
To keep Open WebUI instead, install `hubzoid[openwebui]`, set
`HUBZOID_UI=openwebui`, and configure an Open WebUI administrator through
`HUBZOID_GATEWAY_ADMIN_EMAIL` and `HUBZOID_GATEWAY_ADMIN_PASSWORD`, plus
`WEBUI_AUTH=true` and a stable `WEBUI_SECRET_KEY`.

The gateway writes `gateway-data/deployment.json` and a discovery pointer at
`<hub>/.hubzoid/deployment.json`. All registered hubs—including those with no
grants—appear in the portal. CLI commands find the same operational database
through these pointers. They do not require repeating the database URL.
In Open WebUI mode, the internal authentication URL is discovered from the same manifest.
Protect this file: a database URL can contain credentials (files are mode 0600).

**Hub folder names must be unique (case-insensitively) within one deployment.**
Each hub's directory name is its Casbin access domain, so two hubs that resolve
to the same name (for example `team.alpha` and `team-alpha`, or `Finance` and
`finance`) would share one access domain and could leak grants across agents. The
gateway refuses to start—and writes no manifest—when it detects a collision, and
`deployment.json` validation rejects duplicate, empty or reserved keys. Rename one
of the hub folders before starting the gateway.

For external bridges (`--no-bridges`), mount the same hub/config paths and shared
databases into the bridge processes. Set `HUBZOID_GATEWAY=1` to enable workflows.
Do not configure a different operational database in a hub's `.env`.

Default SQLite layout:

| Data | Owner | Location |
|---|---|---|
| Accounts, sessions, conversations, grants, identities, connector credentials, change history, workflow state | Hubzoid | `gateway-data/hubzoid-operational.db` |
| Accounts, chats and model visibility (Open WebUI mode only) | Open WebUI | `gateway-data/webui.db` |
| Workflow execution history and checkpoints | DBOS | `<hub>/.hubzoid/dbos.db` per hub |
| Tool decisions, usage | Hubzoid | `gateway-data/hubzoid-operational.db` |

This is one **deployment configuration**, not one physical SQLite file. Separate
DBOS files avoid sharing an unsupported multi-application SQLite execution store.
For PostgreSQL, set `DATABASE_URL` before starting the gateway; shared server/DB
storage is supported by the libraries. `HUBZOID_OPERATIONAL_DB` and
`HUBZOID_DBOS_DB` are explicit gateway-level overrides. Multiple hubs must not
share an explicitly configured SQLite DBOS file. A conflicting operational URL
in a registered bridge/CLI fails rather than silently writing another store.
Registered DBOS and internal OWUI URLs follow the same fail-on-conflict rule.
A copied hub pointer is rejected if that hub is not registered in its manifest.

Use one bridge process per hub with embedded SQLite. The two-process test proves
duplicate scheduled-slot suppression against an initialized database, not safe
concurrent first-time SQLite schema creation or production high availability.
Use PostgreSQL for multiple production workers and rehearse deployment upgrades.

## The owner and first access

Every agent's access is managed in the Console: a person may use an agent only
with a grant, however the hub was created or deployed. The configured owner
gets organization administration and entry on every hub at their first verified
sign-in as an administrator: in the web app, in Open WebUI (a Console visit or
their first chat) or through MCP sign-in. With sign-in off, the local owner
(`admin@localhost`) owns every hub from the first start. On a shared
deployment, set `HUBZOID_ADMIN_EMAIL` (web app) or `WEBUI_ADMIN_EMAIL` /
`HUBZOID_GATEWAY_ADMIN_EMAIL` (Open WebUI mode) to the intended administrator.

The verified account must be an administrator and match the configured email.
Other admins and ordinary users receive no automatic grants. Provisioning
happens once per hub: a later login does not restore revoked access.
Organization administration is bootstrapped once per store. Existing chosen
administrators are preserved, including when another hub joins the gateway.
`hubzoid doctor` warns about an agent nobody may use. Never copy `.hubzoid/`
state into a new deployment.

For headless operation or recovery, explicit bootstrap remains available:

```bash
hubzoid access bootstrap --admin operator@example.com ./finance
```

Bootstrap administration does not imply every tool capability. Check **Use this
agent** separately for chat. Keep `HUBZOID_AUTH=true` on shared deployments.
The chat app and Console share one sign-in. Share the chat URL and a new
person's one-time sign-in link; nothing is emailed. In Open WebUI mode, account
changes use its admin API and you share the new account's sign-in details.

## Grant access and verify the end-user experience

1. Open the agent and go to its **Access** tab. For someone without an
   account, select **Add user** and enter their name and email. In the default
   mode they choose their password through a one-time sign-in link. Open
   WebUI mode asks for a password. For an existing user, select **Edit access** on their row,
   or open them under **People** and use **Add an agent**.
2. Tick the capabilities they need (a tool capability includes agent entry
   automatically), then review and save (**Create account** for a new user).
   A new user's one-time link (or Open WebUI sign-in details) is shown once to copy.
3. The permission check changes immediately. The chat app's model picker reflects
   the new access within about 30 seconds; the portal retries that projection on
   its own and only surfaces it if it keeps failing.
4. Ask the person to sign in with that email and open the agent. They should see
   only their permitted tools. Refresh the model picker if it was already open.
5. Review **Activity → Access changes** for who changed what and **Tool
   decisions** for actual allow/deny decisions. Both are scoped to the administrator.
   Filter by time range, agent, person, who-changed-it/action (access changes), or
   tool/outcome/channel (tool decisions); filters and paging are kept in the URL, so a
   filtered view is shareable and survives refresh and Back. **Details** on any row opens
   the full record — precise time (in your timezone), actor, affected identity, agent,
   capability/tool, channel and reason, with copyable identifiers. **People** has the
   same filtering by account status, role and agent.

The portal's **People** screen distinguishes accounts awaiting signup, pending
approval, active accounts, services, and blocked people. **Refresh accounts**
re-reads the account directory. It does not create accounts. **Add user**
creates one (see [Accounts in the Console](#accounts-in-the-console)).
Organization administrators set a user's **Role** (User or Administrator) in
their details. Agent admins cannot change roles or use an entry revocation to
remove another administrator's rights.

Agent admins also work within a ceiling. In each agent they manage they can
grant or remove only the capabilities they hold there themselves, never
`manage_access`, never their own access and never an organization
administrator's. The server checks this on every change. See
[access management](access-management.md#delegated-management-and-its-ceiling-implemented).

Revoking agent entry also removes that person's direct tool grants in that agent.
An existing "Everyone signed in" grant and organization-level admin rights are
displayed as inherited access. Removing a direct grant does not remove them.

New access for everyone signed in cannot be created, by anyone. An agent that
already had it (carried over by migration) shows an **Everyone signed in** row.
To replace it: add the people who need the agent by name, then an organization
administrator selects **Remove** on that row. The confirmation says how many
chat accounts rely on it alone. See
[access management](access-management.md#everyone-signed-in-no-longer-granted).

To offboard someone, an organization administrator opens them under People
and uses **…** → **Delete user**. That removes every grant, then the chat
account and its chats; Activity history, usage records and published artifacts
are kept. In Open WebUI mode, the person's API keys stop working with the
account, but Open WebUI keeps its stored connection tokens for that account.
The last administrator cannot be made a User or deleted. The Console no longer
offers Block or Reactivate; a user blocked in an earlier release stays blocked
(see [blocked users](access-management.md#blocked-users)).

Scheduled workflows run as ordinary accounts ([workflow-identity.md](workflow-identity.md)),
granted like anyone else. Older `workflow:<function_name>` subjects are kept but
no longer used by runs.
Permission definitions come from `restricted/*.py`; optional display metadata
lives in `identity/permissions.yaml`:

```yaml
ledger:
  label: Read ledger
  description: Read accounting entries for this agent.
  sensitive: true
```

Sensitivity is explicit metadata, not inferred from words such as `prod` in a name.
Permission keys `use_hub` and `manage_access` are reserved by Hubzoid. This file
labels restricted capabilities only; an entry for a built-in such as `curator`
is ignored with a warning. The Console groups capabilities and shows whether
each one's settings are present. See
[capabilities in the Console](access-management.md#capabilities-in-the-console).

## The Agents page

The Console opens on **Agents**, combining five summary cards and the agent
cards below them. Choose the last 24 hours, 7 days or 30 days; Refresh reloads
totals, agents and workflow status. A small update time appears beside Refresh.
Hub administrators see only their agents.

| Number | Meaning |
|---|---|
| Messages | Human messages across chat surfaces, with conversation count below. Background title/suggestion calls are excluded. |
| Users | Sign-in accounts in your scope (the deployment for an organization administrator, accounts with access to your agents for a delegate), whatever the period. Blocked accounts count; legacy service identities and email-only grants do not. Shows unavailable, never 0, if accounts can't be read. |
| Tokens used | Input and output tokens, including background and workflow model calls. |
| Workflow runs | Runs in the selected period, with failed count below when applicable. |
| Approx. cost | USD estimate from reported model cost or token prices; unpriced calls are excluded and marked with `*`. The help icon explains subscription billing and estimates. |

Usage comes from Hubzoid's recorded operational data, not historical chat-app
messages. Open an agent card for access, **Runs & schedules**, or Activity.
Global Runs is no longer a navigation entry; existing direct links still work.

Public email and SSO account registration default to off. Managers create
accounts with **Add user** (on an agent's Access tab or on People) and grant
agent access in the same step.
Open WebUI's **Admin Panel → Users** keeps working unless you hide it (below).
Explicit sign-up overrides and existing persisted OWUI settings remain
operator-controlled. See [authentication](auth.md).

## Accounts in the Console

Implemented in this release. Account management is always available to
organization administrators and delegates once the server is configured for it.
Nothing about existing accounts changes until someone uses it.

Default mode needs no Open WebUI service account or internal URL. Accounts
and access use the Hubzoid operational store.

Additional requirements in **Open WebUI mode**:

- the chat app's internal URL: the gateway manifest's `owui_url`, or
  `OWUI_INTERNAL_URL` under `hubzoid run` (set automatically). A public
  `WEBUI_URL` alone is refused, so account writes never pass the edge.
- `HUBZOID_GATEWAY_ADMIN_EMAIL` and `HUBZOID_GATEWAY_ADMIN_PASSWORD`, the
  deployment's Open WebUI service account (an Open WebUI administrator).
  Delegates never see or need these credentials.

What managers can do:

| Action | Who | Where |
|---|---|---|
| Create a user (role `user`) with initial access | Org admins, and delegates within their ceiling | Agent → Access → Add user, or People → Add user |
| Change an existing user's access | Org admins, and delegates within their ceiling | Agent → Access → Edit access, or People → the user → Edit access / Add an agent |
| Approve a pending signup | Org admins | People → the user → Role |
| Issue a one-time password-reset link (a new password in Open WebUI mode) | Org admins | People → the user → Reset password |
| Make someone an Administrator, or a User again | Org admins | People → the user → Role |
| Delete a user | Org admins | People → the user → … → Delete user |

Every action is audited in **Activity → Access changes** (`account_create`,
`account_approve`, `account_password_reset`, `account_role`, `account_delete`)
without the password. Changing an account's email is not offered. Create a new
account instead, because grants are keyed on the email.

In Open WebUI mode, creating an account and granting access touch two systems that cannot
commit together. If access fails after the account exists, the Console says the
account was created without access, keeps its sign-in details on screen, and
**Try again** grants to that account. A duplicate email changes nothing: the
Console says the user exists and links to them, so you edit their access
instead. Retrying never creates a second account, and no rollback is claimed.
**Administrator** sets Hubzoid administration and the chat app's admin role
together and reports a partial result. Details, **Google sign-in only**
accounts and deletion are in
[access management](access-management.md#add-a-user-implemented).

Organization administrators cannot change their own role or account, or the
service account, from the Console. Use `hubzoid admin` for supported operator
recovery, or Open WebUI's own settings in that mode.

### Access and workflows from chat

Two families of agent tools are off for everyone until granted in the Console,
under **Hubzoid tools**:

- **Access control → Manage access from chat** (`access_tools`, organization
  administrators grant it). A manager can ask the agent who has access, why a
  person has it, and propose access changes and new accounts, from chat, MCP,
  WhatsApp or Telegram. The agent replies with a link to
  `/portal/#/confirm/<id>`. The manager signs in on the web, reviews the exact
  change and confirms it. Nothing applies before that. Links expire after
  `HUBZOID_CHANGE_REQUEST_TTL` seconds (default 900) and work once. See
  [access management](access-management.md#access-tools-in-chat-whatsapp-and-mcp-implemented-off-until-granted).
- **Workflows → See workflows and runs** and **Run and control workflows**
  (`workflows_view`, `workflows_manage`). See
  [workflows](workflows.md#from-chat-and-assistants).

`HUBZOID_ACCESS_TOOLS=false` or `HUBZOID_WORKFLOW_TOOLS=false` in a hub's
`.env` removes a family from that agent.

### Hiding the Open WebUI Users page (Open WebUI mode only)

A gateway set up fresh with Console accounts (no earlier `deployment.json` or
chat-app database, `WEBUI_AUTH=true`, and the service account above) hides Open
WebUI's user management. The gateway records `hide_owui_users: true` in
`deployment.json` and says so when it starts. An existing deployment records
nothing and keeps the Users page, so nothing changes on upgrade. An explicit
`HUBZOID_HIDE_OWUI_USERS=true` or `false` in the gateway (or `hubzoid run`)
environment overrides the recorded value; set `true` on an existing deployment
once Console account management is verified there.

When hidden, Open WebUI's whole Users section is hidden: the Admin Panel and
every `/admin/users` page, the user list and Groups included, open **Settings →
Integrations**. Open WebUI groups decide nothing about agents. Evaluations,
Functions and Settings are unchanged. Browser writes to Open WebUI's account admin API
(`POST /api/v1/auths/add`, `POST /api/v1/users/{id}/update`,
`DELETE /api/v1/users/{id}`) are refused. Public sign-up stays closed either way.

### Open WebUI APIs Hubzoid relies on (Open WebUI mode only)

These must stay reachable on the internal URL. None of them passes the edge.

| API | Used for |
|---|---|
| `POST /api/v1/auths/signin`, `POST /api/v1/auths/signup` | Service-account token (signup only on a fresh gateway) |
| `GET /api/v1/auths/` | Verifying a Console session cookie |
| `GET /api/v1/users/?page=` | Refresh accounts; listing administrators for the last-administrator check |
| `POST /api/v1/auths/add` | Add user |
| `GET /api/v1/users/{id}` | Reading an account before changing it |
| `POST /api/v1/users/{id}/update` | Approve, reset password, the chat app's side of the Administrator role |
| `DELETE /api/v1/users/{id}` | Delete user |
| `/api/v1/groups/...` | A gateway's first-boot model provisioning |
| `/api/v1/models/...` | Model registration and the visibility mirror |

Open WebUI API keys are read from its database (read-only) to verify MCP and
management API callers.

## Open WebUI model visibility

In Open WebUI mode each agent is an Open WebUI model. Hubzoid mirrors who may
use it into the model's access list, so each person sees only their agents.
The bridge still checks every turn; the mirror only shapes the picker. Browser
writes to an agent model's access list are refused at the edge. Run
`hubzoid access sync` to re-project it if it was ever missed.

## Workflow execution and inspection

```bash
hubzoid new workflow daily-report ./finance
hubzoid doctor ./finance
hubzoid schedule run ./finance daily_report --dry-run
hubzoid schedule run ./finance daily_report
hubzoid schedule list ./finance
hubzoid schedule status ./finance
```

Markdown tasks (`schedule/*.md`) and code workflows (`workflows/<name>/*.py`)
both run on each hub's DBOS engine. [workflows.md](workflows.md) covers writing
them, model calls (`hub.call_llm`, `hub.call_agent`, `hub.call_jev`), retries,
idempotency and code changes. For operators:

- Gateway deployments schedule code workflows. A standalone `hubzoid run` needs
  `HUBZOID_SCHEDULES=1`. Markdown tasks run whenever their files exist
  (`HUBZOID_DISABLE_SCHEDULE=1` turns them off).
- Manual dry runs do not import workflow code or start DBOS. Workflow runs
  reject the markdown-only `--timeout`, `--max-rounds` and `--model` options.
- Each workflow and task acts as an ordinary account: `run_as`, else
  `HUBZOID_WORKFLOW_USER`, else the setup owner. `hubzoid schedule list` shows
  which. Grant that account the tool permissions it needs
  ([workflow-identity.md](workflow-identity.md)).
- An agent's **Runs & schedules** tab lists every
  run with its steps, result and error. They are read only. Run, pause, resume
  and cancel with `hubzoid schedule` on the server.
- DBOS holds the execution history. Hubzoid keeps no second run database.
- Before a code change or upgrade, drain queued runs. Runs left from older code
  are cancelled at the next start (see [workflows.md](workflows.md)).

## Troubleshooting

| Symptom | Check |
|---|---|
| Grant has no effect | Correct deployment manifest? Matching signup email? Account blocked? |
| Agent not visible | People screen sync status; service-account credentials; `access sync` |
| Portal denies entry | OWUI sign-in session; Hubzoid `manage_access`; discovered internal OWUI URL |
| Add user says account management isn't set up | Internal OWUI URL (manifest or `OWUI_INTERNAL_URL`, not only `WEBUI_URL`) and `HUBZOID_GATEWAY_ADMIN_EMAIL`/`_PASSWORD` |
| **Google sign-in only** is disabled in Add user | The chat app needs `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` and `OAUTH_MERGE_ACCOUNTS_BY_EMAIL=true` from the environment (not `ENABLE_OAUTH_PERSISTENT_CONFIG=true`). A gateway records this when it starts, so restart it after changing them |
| A delegate's grant is refused as outside their access | They must hold that capability in that agent themselves. An org admin can grant it |
| A confirmation link says the request isn't available | Only the person who proposed it can open it. It expires after `HUBZOID_CHANGE_REQUEST_TTL` seconds and works once |
| Workflow absent/error | `doctor`, Workflow state/error, literal valid schedule/timezone, bridge logs |
| No execution history | Correct hub's DBOS database, workflow enabled, run actually submitted |
| User still has access after revocation | "Everyone signed in" or inherited access; use **Delete user** for offboarding |
| `hubzoid grant '*' ...` is refused | New access for everyone signed in can't be created. Grant named people |

For local UI development only, `HUBZOID_PORTAL_DEV=1` plus
`HUBZOID_PORTAL_DEV_USER=<bootstrapped-admin>` bypasses OWUI session validation.
Never enable this bypass on an exposed deployment.


## Account deletion, replacement and approval

OWUI remains the credential owner. The Console creates, approves, resets and
deletes accounts through OWUI's admin API (see
[Accounts in the Console](#accounts-in-the-console)). A successful directory refresh marks missing or
pending accounts unavailable; signup grants stay pending until the actual account
exists. The verified OWUI account ID is bound on migration, login and API-key use.
If a different account reuses an existing email, direct grants are removed and
agent access is blocked, with an audit event. An organization administrator must
review the account, then unblock it through the management API
(`POST /portal/api/people/block` with `"suspended": false`) and grant access
again, or delete it. The Console has no Reactivate button. An existing
"Everyone signed in" grant applies after unblocking. This also protects chat and MCP entry before the next sync.
If the replaced account was the only administrator, use the documented local
`access bootstrap --admin <new-verified-email>` break-glass path, then verify the
new account. Do not unblock an account solely because its email matches.

Gateway planning isolates each hub's dotenv settings; hub-specific secrets are
not inherited by other hubs, OWUI or the edge. Put deployment-wide OWUI service
credentials in the gateway process environment and the operator CLI environment.
Account refresh failures are visible and do not treat a partial/failed directory
response as a mass account deletion.

## Upstream administration and hub runtime

Open WebUI retains its account, authentication, connection, functions and
automation controls. These belong to that application. Hubzoid workflows are
loaded from the hub folder and are inspected in the Console. Do not configure an
Open WebUI automation expecting it to become a Hubzoid workflow.

Standalone picker visibility is checked directly against Hubzoid permissions at
the edge. Gateway deployments also maintain the Open WebUI visibility mirror.
A mirror warning is not evidence that execution access has been granted.
