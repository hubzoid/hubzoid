# Administering a multi-hub deployment

Hubzoid has two audiences: builders configure agents, tools and workflows;
end users sign in and use the agents they have been granted. Open WebUI owns
accounts and authentication. Hubzoid owns agent/tool permissions and execution
inspection. The Admin Console is at `/portal/`. Organization administrators and people with
Manage access see **Admin Console** above their profile in the Open WebUI sidebar
when using the Hubzoid edge. The collapsed sidebar shows an icon with a tooltip.

## One deployment, several agents

```bash
export WEBUI_AUTH=true
export HUBZOID_GATEWAY_ADMIN_EMAIL=operator@example.com
export HUBZOID_GATEWAY_ADMIN_PASSWORD='your-service-account-password'
hubzoid gateway ./finance ./operations --data-dir ./gateway-data
```

The OWUI service account must be an OWUI administrator. Use the same credentials
in the gateway service environment for provisioning, account lookup and visibility
sync. No email or invitation is sent by the Console. OWUI signup/OIDC and account
approval continue to work as configured in OWUI.

The gateway writes `gateway-data/deployment.json` and a discovery pointer at
`<hub>/.hubzoid/deployment.json`. All registered hubs—including those with no
grants—appear in the portal. CLI commands find the same operational database
through these pointers. They do not require repeating the database URL.
The internal OWUI authentication URL is discovered from the same manifest.
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
| Accounts, sessions, chats, model visibility | OWUI | `gateway-data/webui.db` |
| Grants, identities, change history, workflow state | Hubzoid | `gateway-data/hubzoid-operational.db` |
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

## Fresh installation

New hubs created with `hubzoid init` carry an optional local installation marker.
At the first verified owner session, Hubzoid grants organization administration
and entry and activates managed access for those fresh hubs. On a local,
auth-disabled standalone hub this is `admin@localhost`. On a shared deployment,
set `WEBUI_ADMIN_EMAIL` or `HUBZOID_GATEWAY_ADMIN_EMAIL` to the intended Open WebUI
administrator. Sign in with that account and open chat or `/portal/`.

The verified account must have Open WebUI's admin role and match the configured
email. Other admins and ordinary users receive no automatic grants. The
provisioning marker is durable: a later login does not restore revoked access.
Organization administration is bootstrapped once per store. Existing chosen
administrators are preserved, including when another hub joins the gateway.
Existing hubs keep their authority mode until migration; initializing an existing
hub does not mark it fresh. Never copy `.hubzoid/` state into a new deployment.

For headless operation or recovery, explicit bootstrap remains available:

```bash
hubzoid access bootstrap --admin operator@example.com ./finance
# Activate only a fresh hub, or use the migration procedure for an existing hub.
hubzoid access bootstrap --authoritative ./finance
```

Bootstrap administration does not imply every tool capability. Check **Use this
agent** separately for chat. Keep `WEBUI_AUTH=true` on shared deployments.
Credentials and sign-in stay in Open WebUI; there is no second Console
credential. The Console creates and changes accounts through Open WebUI's admin
API. Nothing sends an invitation: share the chat URL and, for an account you
created, its sign-in details yourself.

## Grant access and verify the end-user experience

1. Open the agent, go to its **Access** tab and select **Add user**. Choose
   **Existing account** and find the person, or **New account** to create their
   sign-in in the same step (name, email, and a password you type or
   generate).
2. Tick the capabilities they need (a tool capability includes agent entry
   automatically), then **Review changes** and save (**Create account** for a
   new account). A new account's sign-in details are shown once to copy.
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
Organization admins add or remove other organization admins from a person's
**Details**. Agent admins cannot change admin rights or use an entry revocation
to remove another admin's rights.

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

**Block access** removes a person's direct grants and blocks agent access,
including access through "Everyone signed in". It leaves the OWUI account and chats intact. Reactivation
does not restore removed grants. For full account offboarding, use **Delete
account** in the person's Details (organization administrators), which removes
every grant and then the chat account and its chats. The person's Open WebUI API
keys stop working with the account. Open WebUI keeps its stored connection tokens
for a deleted account in its database. The final organization administrator
cannot be removed, blocked or deleted.

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
| Users | People who sent a message in the selected period, not everyone granted access. |
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
Nothing about existing accounts or legacy agents changes until someone uses it.

Requirements:

- the chat app's internal URL: the gateway manifest's `owui_url`, or
  `OWUI_INTERNAL_URL` under `hubzoid run` (set automatically). A public
  `WEBUI_URL` alone is refused, so account writes never pass the edge.
- `HUBZOID_GATEWAY_ADMIN_EMAIL` and `HUBZOID_GATEWAY_ADMIN_PASSWORD`, the
  deployment's Open WebUI service account (an Open WebUI administrator).
  Delegates never see or need these credentials.

What managers can do:

| Action | Who | Where |
|---|---|---|
| Create an account (role `user`) with initial access | Org admins, and delegates within their ceiling | Agent → Access → Add user → New account, or People → Add user |
| Give an existing account access | Org admins, and delegates within their ceiling | Agent → Access → Add user → Existing account |
| Approve a pending signup | Org admins | Person Details → Chat account |
| Reset a password (shown once) | Org admins | Person Details → Chat account |
| Switch the chat-app admin role | Org admins | Person Details → Chat account |
| Delete an account | Org admins | Person Details → Chat account |

Every action is audited in **Activity → Access changes** (`account_create`,
`account_approve`, `account_password_reset`, `account_role`, `account_delete`)
without the password. Changing an account's email is not offered. Create a new
account instead, because grants are keyed on the email.

Creating an account and granting its access touch two systems that cannot
commit together. If access fails after the account exists, the Console says the
account was created without access, keeps its sign-in details on screen, and
**Try again** grants to that account. A duplicate email offers **Grant access
instead**. Retrying never creates a second account, and no rollback is claimed.
Details, and **Google sign-in only** accounts, are in
[access management](access-management.md#add-a-user-implemented).

Organization administrators cannot change their own account or the service
account from the Console. For those, use Open WebUI's own settings or the server.

### Proposals from agents

With `HUBZOID_MANAGEMENT_TOOLS=true` in a hub's `.env` (default off), that hub's
agent can propose access changes and new accounts on behalf of the signed-in
manager, from chat, MCP, WhatsApp or Telegram. It replies with a link to
`/portal/#/confirm/<id>`. The manager signs in on the web, reviews the exact
change and confirms it. Nothing applies before that. Links expire after
`HUBZOID_CHANGE_REQUEST_TTL` seconds (default 900) and work once. See
[access management](access-management.md#proposals-from-chat-whatsapp-and-mcp-implemented-off-by-default).

### Hiding the Open WebUI Users page

A gateway set up fresh with Console accounts (no earlier `deployment.json` or
chat-app database, `WEBUI_AUTH=true`, and the service account above) hides Open
WebUI's user management. The gateway records `hide_owui_users: true` in
`deployment.json` and says so when it starts. An existing deployment records
nothing and keeps the Users page, so nothing changes on upgrade. An explicit
`HUBZOID_HIDE_OWUI_USERS=true` or `false` in the gateway (or `hubzoid run`)
environment overrides the recorded value; set `true` on an existing deployment
once Console account management is verified there.

When hidden, Open WebUI's user list (`/admin/users/overview`) opens Console
People, and its Users section (`/admin/users`, where the Admin Panel opens)
lands on Groups, which stays for legacy agents. Evaluations, Functions and
Settings are unchanged. Browser writes to Open WebUI's account admin API
(`POST /api/v1/auths/add`, `POST /api/v1/users/{id}/update`,
`DELETE /api/v1/users/{id}`) are refused. Public sign-up stays closed either way.

### Open WebUI APIs Hubzoid relies on

These must stay reachable on the internal URL. None of them passes the edge.

| API | Used for |
|---|---|
| `POST /api/v1/auths/signin`, `POST /api/v1/auths/signup` | Service-account token (signup only on a fresh gateway) |
| `GET /api/v1/auths/` | Verifying a Console session cookie |
| `GET /api/v1/users/?page=` | Refresh accounts |
| `GET /api/v1/users/?query=` | Finding an account by email for Add user's existing-account path |
| `POST /api/v1/auths/add` | Add user (new account) |
| `GET /api/v1/users/{id}` | Reading an account before changing it |
| `POST /api/v1/users/{id}/update` | Approve, reset password, chat-app role |
| `DELETE /api/v1/users/{id}` | Delete account |
| `/api/v1/groups/...` | Group provisioning and the legacy visibility mirror |
| `/api/v1/models/...` | Model registration and the visibility mirror |

Open WebUI API keys are read from its database (read-only) to verify MCP and
management API callers.

## Migrate existing customers

Migration keeps the existing preview and atomic cutover. Start with a clone.
Keep the current gateway version/database backups until end-user checks pass.

1. Back up with `hubzoid backup <hub>` (it covers the whole gateway: manifest,
   OWUI database, operational database and each hub's state). For PostgreSQL,
   also take a `pg_dump`. See [BACKUP.md](BACKUP.md).
2. On the clone, export any function-backed access roster to an explicit CSV;
   remove the live access function before migrating. Computed permissions cannot
   be safely enumerated automatically.
3. Preview each hub with its specific OWUI model ID:

```bash
hubzoid access migrate ./finance \
  --from-owui sqlite:////absolute/gateway-data/webui.db --model-id finance
```

The model ID is the gateway's configured model label, not necessarily the folder
name. The OWUI 0.11 adapter reads `group_member` and `access_grant`; supported older
JSON schemas are recognized explicitly. Unknown schemas are refused. Import combines
CSV and OWUI tool groups, intersected with existing model visibility. It checks a
before/after permission matrix containing both permitted and denied users before
activation. Preview prints the number of legacy decisions checked and any
differences. A CSV-only preview warns that model visibility has not been verified, and
`--apply` refuses an unverified plan. For a legacy standalone hub where signed-in
entry was public, pass `--standalone-public` explicitly. Public legacy entry
is preserved as an "Everyone signed in" grant, and the report says
`Everyone signed in (carried over)`. It is the only way such a grant is still
written. It checks tool permissions
against the existing CSV resolver and automatically includes the local OWUI group
source when present; `--from-owui` can specify a different source copy. Registered
gateways require model ACL evidence and reject standalone bypass. Disabled models are refused rather than implicitly enabled.
It does not translate legacy groups into administrator privileges.

4. Freeze access edits, rerun the preview against the live source, then cut over:

```bash
hubzoid access migrate ./finance \
  --from-owui sqlite:////absolute/gateway-data/webui.db --model-id finance --apply
hubzoid access sync ./finance
hubzoid access diff ./finance \
  --from-owui sqlite:////absolute/gateway-data/webui.db --model-id finance
```

`--apply` automatically writes a mode-0600 pre-cutover access snapshot under
`<hub>/.hubzoid/backups/`. Grants and activation change in one transaction.
Test representative end users' agent entry and restricted tools immediately.
The static diff compares the imported plan with stored grants; it supplements,
not replaces, the effective access checks and real user checks.

5. If needed, restore the printed snapshot:

```bash
hubzoid access rollback /absolute/finance/.hubzoid/backups/access-....json ./finance
```

Rollback restores that hub's grants, attributes and previous authority state.
For an OWUI-based migration, the snapshot also retains the original model ACL.
Rollback to legacy restores it through OWUI's API, preserving current model
settings. Pause the visibility sync worker and access edits during rollback so an
in-flight projection cannot overwrite the restored ACL. If OWUI is unavailable,
the command reports partial restoration and exits unsuccessfully: keep maintenance
open, fix connectivity/credentials, then rerun the same rollback. Older or CSV-only
snapshots lack that ACL; the command explicitly requires restoring it from your
pre-cutover deployment backup. For a previously managed hub, run `access sync`.
Rollback does not undo account changes or replace a full deployment backup.

Migrate one hub at a time. Shared OWUI groups remain editable for unmigrated hubs
and non-agent resources. Migrated models' ACL writes are blocked at the edge and
redirect operators to the portal. Chat permissions always come from the authority
for the selected hub; OWUI visibility is only a mirror. OWUI's own administrators
may retain its privileged model visibility, but Hubzoid still checks agent entry.

## Planned maintenance upgrade (the whole deployment)

The upgrade is a short, explicit maintenance window run by an operator — there is no
automatic migration on startup. It reuses the commands above (`access migrate`,
`access sync`, `access diff`, `access rollback`) in this sequence. Do it once, per hub,
with all access writers stopped.

Prerequisites you must have to hand:
- The OWUI source database for each hub (SQLite file path, or a PostgreSQL URL if OWUI
  runs on Postgres) and each hub's **OWUI model ID** (the gateway's configured model
  label, not necessarily the folder name).
- The gateway admin email/password (`HUBZOID_GATEWAY_ADMIN_EMAIL` / `_PASSWORD`) so the
  visibility sync can sign in to OWUI.
- The email(s) of the existing OWUI administrator(s) who will own the dashboard.

Procedure:

1. **Stop the writers, but keep Open WebUI reachable.** Stop all bridges and the
   visibility projector (nothing should edit access or project during the window). The
   gateway normally supervises the OWUI subprocess, so stopping the gateway also stops
   OWUI — but `access sync` and rollback's visibility restore call OWUI's API. So run
   **OWUI privately** for the window: start it on its own, bound to loopback
   (`127.0.0.1`), with `OWUI_INTERNAL_URL`/`WEBUI_URL` and `HUBZOID_GATEWAY_ADMIN_EMAIL`
   / `_PASSWORD` set so the CLI can sign in — but with the bridges and projector down so
   no chat traffic or projection races the migration.
2. **Back up.** Run `hubzoid backup <hub>` and keep the archive in a protected location.
   For PostgreSQL, also take a `pg_dump` ([BACKUP.md](BACKUP.md)).
   (`access migrate --apply` also writes its own 0600 pre-cutover snapshot under
   `<hub>/.hubzoid/backups/`, but keep your full backup too — it is the verification and
   rollback baseline, and it captures the ORIGINAL OWUI model visibility before any sync.)
3. **Establish dashboard admins**, once, explicitly (audited):
   ```bash
   hubzoid access bootstrap --admin admin@example.org ./finance
   ```
4. **Preview, migrate, and verify BEFORE projecting**, per hub. Preview is a dry-run; it
   checks a before/after matrix of permitted AND denied users and refuses an unverified or
   empty plan. Run the confirming `diff` **immediately after `--apply`, before `sync`** —
   `diff` rebuilds its baseline by reading the OWUI source, and `sync` will rewrite that
   source's model visibility (replacing "public" with explicit per-user grants), so a
   `--from-owui` diff run *after* sync compares against a changed baseline and can report
   a false mismatch even though Hubzoid's permissions are unchanged.
   ```bash
   hubzoid access migrate ./finance \
     --from-owui sqlite:////absolute/gateway-data/webui.db --model-id finance           # preview (dry-run)
   hubzoid access migrate ./finance \
     --from-owui sqlite:////absolute/gateway-data/webui.db --model-id finance --apply    # cut over (+ 0600 backup)
   hubzoid access diff ./finance \
     --from-owui sqlite:////absolute/gateway-data/webui.db --model-id finance            # verify: expect 0 missing, 0 extra
   ```
   Re-running `--apply` on an already-migrated hub is refused (it would overwrite edits
   made since migration); `--remigrate` overrides that only if you intend to discard them.
5. **Project visibility, then verify the projection separately** (not with another
   `--from-owui` diff — see above). `sync` is idempotent, so a clean second run is the
   check that projection converged:
   ```bash
   hubzoid access sync ./finance   # projects Casbin → OWUI model visibility
   hubzoid access sync ./finance   # run again: it should report state "ok" and change nothing
   ```
6. **Restart** the gateway and bridges (which resumes the normal visibility loop).
7. **Smoke-test:** an admin opens the dashboard (the "Manage agent access" link in Open
   WebUI) and sees the migrated agent as editable; a permitted user can enter the agent
   and use its tools in chat; a denied user cannot; an ordinary user still uses chat
   normally; visibility sync shows `ok`.

**Rollback (if a hub fails verification):** with the bridges/projector still stopped and
OWUI still running privately (step 1), restore that hub's pre-cutover snapshot — this
restores grants, authority, AND the original OWUI model visibility saved in the backup.
Restart leaves the rollback intact (the hub is legacy again). If OWUI is unreachable the
command reports partial restoration and exits non-zero — keep the window open, fix
connectivity, rerun the same command.
```bash
hubzoid access rollback /absolute/finance/.hubzoid/backups/access-....json ./finance
```

**After upgrade — where things live:** migrated agents' permissions are managed in the
dashboard (the portal refuses and hides permission edits for a still-legacy agent, so
nothing there is misleading or silently overwritten); accounts, sign-in and roles stay
in Open WebUI. Migration flattens OWUI groups into direct grants, so if onboarding adds
users to an OWUI group to grant agent access, after migration those new members need an
explicit dashboard grant instead — confirm whether the deployment relies on group-based
onboarding before scheduling the window.

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
| Grant has no effect | Agent still marked legacy? Correct deployment manifest? Matching signup email? |
| Agent not visible | People screen sync status; service-account credentials; `access sync` |
| Portal denies entry | OWUI sign-in session; Hubzoid `manage_access`; discovered internal OWUI URL |
| Add user says account management isn't set up | Internal OWUI URL (manifest or `OWUI_INTERNAL_URL`, not only `WEBUI_URL`) and `HUBZOID_GATEWAY_ADMIN_EMAIL`/`_PASSWORD` |
| Add user has no **Google sign-in only** choice | The chat app needs `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` and `OAUTH_MERGE_ACCOUNTS_BY_EMAIL=true` from the environment (not `ENABLE_OAUTH_PERSISTENT_CONFIG=true`). A gateway records this when it starts, so restart it after changing them |
| A delegate's grant is refused as outside their access | They must hold that capability in that agent themselves. An org admin can grant it |
| A confirmation link says the request isn't available | Only the person who proposed it can open it. It expires after `HUBZOID_CHANGE_REQUEST_TTL` seconds and works once |
| Workflow absent/error | `doctor`, Workflow state/error, literal valid schedule/timezone, bridge logs |
| No execution history | Correct hub's DBOS database, workflow enabled, run actually submitted |
| User still has access after revocation | "Everyone signed in" or inherited access; use Block access for offboarding |
| `hubzoid grant '*' ...` is refused | New access for everyone signed in can't be created. Grant named people or workflow identities |

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
review the account, reactivate it and grant access again. An existing
"Everyone signed in" grant applies after reactivation. This also protects chat and MCP entry before the next sync.
If the replaced account was the only administrator, use the documented local
`access bootstrap --admin <new-verified-email>` break-glass path, then verify the
new account. Do not reactivate an account solely because its email matches.

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
