# Administering a multi-hub deployment

One gateway can serve several agents. Builders configure agents, tools and
workflows. Everyone else signs in and uses the agents they have been granted.
Hubzoid owns accounts, sign-in, conversations, permissions and execution
history, and administrators manage them in the web app's **Settings**, under
Team & deployment. The old Console address, `/portal/`, opens the same
section.

## Example

```bash
export HUBZOID_AUTH=true
export HUBZOID_ADMIN_EMAIL=operator@example.com
export HUBZOID_ADMIN_PASSWORD='your-strong-bootstrap-password'
hubzoid gateway ./finance ./operations --data-dir ./gateway-data
```

On a fresh deployment this creates the first administrator. Sign in with that
account, then add people in **Settings**, **People & access**. Hubzoid gives
one-time sign-in links and sends no email. See [authentication](auth.md).

## Settings

| Variable | Default | What it does |
|---|---|---|
| `HUBZOID_AUTH` | `false` (local mode) | `true` requires sign-in. Keep it on for a shared deployment. |
| `HUBZOID_ADMIN_EMAIL` | | The owner. Their first verified sign-in as an administrator gets organization administration and entry on every hub. |
| `DATABASE_URL` | SQLite in `--data-dir` | PostgreSQL for the operational store. Set it before starting the gateway. |
| `HUBZOID_OPERATIONAL_DB`, `HUBZOID_DBOS_DB` | | Gateway-level overrides for the operational and workflow databases. |
| `HUBZOID_AUDIT` | `off` | `db` or `both` keeps tool calls and sign-in events for **Activity**. `log` writes JSON lines only. See [the audit log](AUDIT.md). |
| `HUBZOID_AUDIT_RETENTION_DAYS` | `90` | How long audit records stay in the database. |
| `HUBZOID_CHANGE_REQUEST_TTL` | `900` | Seconds a confirmation link from chat stays valid (60 to 86400). |
| `HUBZOID_ACCESS_TOOLS`, `HUBZOID_WORKFLOW_TOOLS` | on | `false` in a hub's `.env` removes that family of tools from the agent. |
| `HUBZOID_SCHEDULES` | on in a gateway | `1` turns on code workflow schedules for a standalone `hubzoid run`. |
| `HUBZOID_DISABLE_SCHEDULE` | | `1` turns off Markdown tasks (`schedule/*.md`). |
| `HUBZOID_WORKFLOW_USER` | the setup owner | The account a workflow runs as when it has no `run_as`. |

## Where data lives

The gateway writes `gateway-data/deployment.json` and a pointer at
`<hub>/.hubzoid/deployment.json`. CLI commands find the operational database
through these pointers, so you do not repeat the database URL. Protect these
files (mode 0600): a database URL can contain credentials.

Default SQLite layout:

| Data | Owner | Location |
|---|---|---|
| Accounts, sessions, conversations, grants, identities, connector credentials, change history, workflow state, tool decisions, usage, audit records | Hubzoid | `gateway-data/hubzoid-operational.db` |
| Workflow execution history and checkpoints | DBOS | `<hub>/.hubzoid/dbos.db`, one per hub |

- Separate DBOS files avoid sharing one SQLite execution store between
  applications. Multiple hubs must not share an explicitly configured SQLite
  DBOS file.
- With PostgreSQL, shared server storage is supported. Use it for multiple
  production workers and rehearse upgrades.
- A bridge or CLI that names a different operational or DBOS URL than the
  manifest fails rather than writing to another store. Do not configure a
  different operational database in a hub's `.env`.
- Each hub's `.env` settings stay with that hub. Hub secrets are not
  inherited by other hubs or the edge.
- A copied hub pointer is rejected if that hub is not registered in its
  manifest. Never copy `.hubzoid/` state into a new deployment.
- For external bridges (`--no-bridges`), mount the same hub paths and
  databases into the bridge processes, and set `HUBZOID_GATEWAY=1` to enable
  workflows.
- Use one bridge process per hub with embedded SQLite. The two-process test
  proves duplicate scheduled-slot suppression against an initialized
  database, not safe concurrent first-time SQLite schema creation or high
  availability.

**Hub folder names must be unique, ignoring case.** Each folder name is the
hub's access domain, so `team.alpha` and `team-alpha`, or `Finance` and
`finance`, would share grants. The gateway refuses to start, and writes no
manifest, when names collide. Rename one folder first.

## The owner and first access

A person may use an agent only with a grant, however the hub was created. The
configured owner (`HUBZOID_ADMIN_EMAIL`) gets organization administration and
entry on every hub at their first verified sign-in as an administrator, in
the web app or through MCP sign-in. With sign-in off, the local owner
(`admin@localhost`) owns every hub from the first start.

- Other administrators and users get no automatic grants.
- Provisioning happens once per hub. A later sign-in does not restore revoked
  access. Existing administrators are kept when another hub joins.
- `hubzoid doctor` warns about an agent nobody may use.
- Bootstrap administration does not include every tool capability. Grant
  **Use this agent** separately for chat.

For headless operation or recovery:

```bash
hubzoid access bootstrap --admin operator@example.com ./finance
```

## Grant access to an agent

1. Open the agent and go to its **Access** tab. For someone without an
   account, select **Add user** and enter their name and email. For an
   existing user, select **Edit access** on their row, or open them under
   **People** and use **Add an agent**.
2. Tick the capabilities they need (a tool capability includes agent entry),
   then save (**Create account** for a new user). A new user's one-time
   sign-in link is shown once to copy.
3. The permission check changes at once, and the person's agent list follows.
4. Ask the person to sign in and open the agent. They should see only their
   permitted tools.
5. Check **Activity**, **Access changes** for the change.

Limits on who can grant what:

- Agent admins grant or remove only capabilities they hold in that agent
  themselves, never `manage_access`, never their own access and never an
  organization administrator's. The server checks every change. See
  [access management](access-management.md#delegated-management-and-its-ceiling-implemented).
- Only organization administrators set a user's **Role** (User or
  Administrator).
- Revoking agent entry also removes that person's direct tool grants in that
  agent. Inherited access ("Everyone signed in", organization admin rights)
  stays.
- New access for everyone signed in cannot be created. An agent that already
  had it shows an **Everyone signed in** row. To replace it, add the people by
  name, then an organization administrator selects **Remove** on that row.
  See [access management](access-management.md#everyone-signed-in-no-longer-granted).

**People & access** shows accounts awaiting signup, pending approval, active,
services and blocked. **Refresh accounts** re-reads the account directory
and creates nothing.

Scheduled workflows run as ordinary accounts, granted like anyone else (see
[workflow identity](workflow-identity.md)). Permission definitions come from
`restricted/*.py`. Optional labels live in `identity/permissions.yaml`:

```yaml
ledger:
  label: Read ledger
  description: Read accounting entries for this agent.
  sensitive: true
```

Sensitivity is explicit, never guessed from a name. `use_hub` and
`manage_access` are reserved. An entry for a built-in such as `curator` is
ignored with a warning. See
[capabilities in Settings](access-management.md#capabilities-in-settings).

## Activity

**Settings**, **Activity** shows what happened, scoped to the agents you
manage.

| Tab | What it lists | Who sees it |
|---|---|---|
| Access changes | Who changed access, for whom and to what | Administrators |
| Tool decisions | Every restricted tool call, allowed or denied | Administrators |
| Tool calls | Every tool call: who, which tool, allowed or refused, how it ended | Administrators, with `HUBZOID_AUDIT=db` or `both` |
| Sign-in events | Failed sign-ins, lockouts and personal API key use | Organization administrators, with `HUBZOID_AUDIT=db` or `both` |

- Filter by time (24 hours, 7 days, 30 days), agent and person. Access
  changes add who changed it and the action. Tool decisions and tool calls add
  outcome, tool and channel, and tool calls add the result.
- Filters and the page stay in the address, so a filtered view can be shared
  and survives refresh and Back.
- Select a row for the full record: exact time in your timezone, actor,
  affected person, agent, capability or tool, channel and reason, with
  copyable identifiers.
- **Export** downloads the stored audit records of that kind for the chosen
  agent and time, as JSON lines. Access changes and sign-in events need an
  organization administrator.

**People** has the same filtering by account status, role and agent.

## The Agents page

**Settings**, **Agents** lists every registered agent: what it is built with,
people with access, and chats, spend and errors in the last 30 days, when it
was last used and its status. These columns always count 30 days, whatever
period the Usage page shows.

Search by name, filter to **Needs attention** (a workflow in error or stale)
or **Not used yet**, and page ten at a time. Hub administrators see only their
agents. Select an agent to open its **Access** tab. Its workflow and eval tags
open **Runs & schedules** and **Evals**.

## The Usage page

**Settings**, **Usage** shows what agents used, how they ran and their
limits. One period (24 hours, 7 days or 30 days, default 7 days) and an agent
filter drive every section. Both stay in the address, so a link opens the
same view. Hub administrators see only their agents.

| Section | What it shows |
|---|---|
| Totals | Messages (and conversations), people active (and accounts in your scope), tokens (and the share read from the prompt cache), approximate cost (and calls without a known price), workflow runs (failed runs and missed schedule slots), and the error rate of recorded calls (and tools denied). Each total shows the change against the previous period of the same length, hidden when that period was not fully recorded. |
| Tokens or cost over time | Hourly for 24 hours, daily otherwise: input, input read from cache and output. Switch between tokens and cost, and between chart and table. Days before recording started are marked. |
| By channel | Calls and cost per surface: web chat, Slack, WhatsApp, Telegram, MCP, workflows, API. |
| How it was used | Share of calls by what started them: chat, schedules, run by hand, events, MCP, evals. Calls recorded before 2.0.9 show as "Earlier, not tracked". |
| By agent | Messages, people, tokens, cost, errors, p50 response time and the monthly limit bar for each agent. Searchable and sortable, ten a page. Open a row to set its limits ([usage limits](budgets.md)). Limits always count the calendar month. |
| Response time | Time to the first words of a web chat reply (p50), and the full reply or model call at p50 and p95, over time. Failed and background calls are left out. |
| By model | Calls, input, share from cache, output, reasoning tokens and cost for the model that answered. A turn's background and sub-agent calls count with it. Claude counts thinking inside output, so its reasoning reads "In output". Models with no known price are flagged. |
| By person | People, schedules and workflows with the agents they used, messages, tokens, cost and last activity. Searched, sorted and paged on the server, eight a page. |

Numbers come from Hubzoid's usage records (`hz_usage`), one row per chat turn
or model call, with no message content. Costs are estimates from model
prices. The provider's bill is final. Before 2.0.9 the per-agent rows showed
the calendar month whatever period was chosen. They now follow the period.

### Read usage for one agent

Pick the agent in the filter, or open the page with the agent in the address.
The same data is available from the API, for an administrator:

```
GET /portal/api/usage?period=7d&hub=<agent>
GET /portal/api/usage/people?period=7d&hub=<agent>&q=&sort=cost&order=desc&page=0&size=8
```

| Endpoint | Parameters | Returns |
|---|---|---|
| `/portal/api/usage` | `period` (`24h`, `7d`, `30d`, default `7d`), `hub` (optional) | `totals`, `previous`, `series`, `by_surface`, `by_trigger`, `by_model`, `by_hub`, `latency`, `recording_since` |
| `/portal/api/usage/people` | `period`, `hub`, `q`, `sort` (`cost`, `tokens`, `messages`, `name`, `last`), `order` (`asc`, `desc`), `page`, `size` (1 to 100, default 8) | `rows`, `total` |
| `/portal/api/summary` | `period` | Workflow runs, missed slots, denied tools, accounts and per-agent rows (including `errors`) |

All are scoped to the agents the caller manages. An agent outside that scope
answers 404. Another period answers 422.

## Accounts in Settings

Organization administrators and delegates manage accounts in Settings.
Accounts and access live in the Hubzoid operational store, with no service
account or second system. Public email and SSO registration are off by
default. Managers create accounts with **Add user** and grant access in the
same step. Sign-up settings stay with the operator (see
[authentication](auth.md)).

| Action | Who | Where |
|---|---|---|
| Create a user (role `user`) with initial access | Org admins, and delegates within their ceiling | Agent, Access, Add user, or People, Add user |
| Change an existing user's access | Org admins, and delegates within their ceiling | Agent, Access, Edit access, or People, the user, Edit access or Add an agent |
| Approve a pending signup | Org admins | People, the user, Role |
| Issue a one-time password-reset link | Org admins | People, the user, Reset password |
| Make someone an Administrator, or a User again | Org admins | People, the user, Role |
| Delete a user | Org admins | People, the user, **...**, Delete user |
| Reactivate a blocked user | Org admins | People, the user, **...**, Reactivate |

- Every action is recorded in **Activity**, **Access changes**
  (`account_create`, `account_approve`, `account_password_reset`,
  `account_role`, `account_delete`), without the password.
- Changing an account's email is not offered. Grants are keyed on the email,
  so create a new account instead.
- A duplicate email changes nothing: Settings links to the existing user. If
  granting fails after the account exists, **Try again** grants to that
  account and never creates a second one. See
  [access management](access-management.md#add-a-user-implemented).
- **Delete user** removes every grant and ends every sign-in, API key and
  connection. Chats, Activity history, usage records and published artifacts
  are kept under their name. Their share links stop working.
- The last administrator cannot be made a User or deleted. Settings does not
  offer Block (see [blocked users](access-management.md#blocked-users)).
- Organization administrators cannot change their own role or account from
  Settings. Use `hubzoid admin` for operator recovery.

### Replaced accounts and recovery

Settings creates, approves, resets and deletes accounts. A successful
directory refresh marks missing or pending accounts unavailable, and signup
grants stay pending until the account exists. A failed or partial refresh is
shown and never treated as a mass deletion.

If a different account reuses an existing email, its direct grants are
removed and agent access is blocked, with an audit event. An organization
administrator reviews the account, then chooses **Reactivate** (or calls
`POST /portal/api/people/block` with `"suspended": false`) and grants access
again, or deletes it. Do not unblock an account only because its email
matches. If the replaced account was the only administrator, run
`hubzoid access bootstrap --admin <new-verified-email>`, then verify the new
account.

### Access and workflows from chat

Two families of agent tools are off for everyone until granted in Settings,
under **Hubzoid tools**:

- **Access control, Manage access from chat** (`access_tools`, granted by
  organization administrators). A manager asks the agent who has access and
  why, and proposes access changes and new accounts, from chat, MCP, WhatsApp
  or Telegram. The agent replies with a link to `/settings#/confirm/<id>`.
  The manager signs in, reviews the exact change and confirms it. Nothing
  applies before that. Links work once, only for the person who proposed the
  change, and expire after `HUBZOID_CHANGE_REQUEST_TTL` seconds. See
  [access management](access-management.md#access-tools-in-chat-whatsapp-and-mcp-implemented-off-until-granted).
- **Workflows, See workflows and runs** and **Run and control workflows**
  (`workflows_view`, `workflows_manage`). See
  [workflows](workflows.md#from-chat-and-assistants).

## Workflows

```bash
hubzoid new workflow daily-report ./finance
hubzoid doctor ./finance
hubzoid schedule run ./finance daily_report --dry-run
hubzoid schedule run ./finance daily_report
hubzoid schedule list ./finance
hubzoid schedule status ./finance
```

Markdown tasks (`schedule/*.md`) and code workflows (`workflows/<name>/*.py`)
both run on each hub's DBOS engine. [Workflows](workflows.md) covers writing
them. For operators:

- A gateway schedules code workflows. A standalone `hubzoid run` needs
  `HUBZOID_SCHEDULES=1`. Markdown tasks run whenever their files exist.
- Manual dry runs do not import workflow code or start DBOS. Workflow runs
  reject the Markdown-only `--timeout`, `--max-rounds` and `--model` options.
- Each workflow and task runs as an ordinary account: `run_as`, else
  `HUBZOID_WORKFLOW_USER`, else the setup owner. `hubzoid schedule list` shows
  which. Grant that account the tools it needs.
- An agent's **Runs & schedules** tab lists runs and steps, with controls
  gated by the viewer's workflow permissions. Personal results belong to the
  account that ran them. Administrator access alone does not reveal them.
- DBOS holds the execution history. Hubzoid keeps no second run database.
- Before a code change or upgrade, drain queued runs. Runs left from older
  code are cancelled at the next start.

### Personal reports and schedules

Grant **Use this agent**, the specific **Run &lt;report&gt;** capability and,
optionally, **Schedule reports for themselves**. The workflow must opt into
chat execution and personal scheduling. A person can then find reports and
their recent results, and edit, pause or stop their own schedules under
**Settings**, **Schedules**, without workflow administration rights.
Administrators can inspect, pause or stop another person's schedule, which
does not grant access to its report contents. Email notices need explicit
opt-in. See [reports and email](reports-and-email.md).

## Local UI development

For local UI development only, `HUBZOID_PORTAL_DEV=1` with
`HUBZOID_PORTAL_DEV_USER=<bootstrapped-admin>` bypasses session validation for
the management API. Never turn this on for an exposed deployment.

## Troubleshooting

| Symptom | Check |
|---|---|
| Grant has no effect | Correct deployment manifest? Matching signup email? Account blocked? |
| Agent not visible | The person's **Use this agent** grant, a blocked account, `hubzoid doctor` |
| Settings shows no Team & deployment sections | The account needs organization administration or Manage access on an agent |
| **Tool calls** or **Sign-in events** is empty with a note about `HUBZOID_AUDIT` | Set `HUBZOID_AUDIT=db` or `both` in the deployment's settings. Earlier events were not kept |
| **Sign-in events** or an export of access changes is refused | It needs an organization administrator |
| Usage shows no change against the previous period | Recording started inside that previous period, so there is nothing to compare |
| The start stops naming an Open WebUI setting | Rename or remove it ([upgrading to 2.0](UPGRADING.md#upgrading-to-20-open-webui-removed)) |
| **Google sign-in only** is disabled in Add user | The web app needs `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` and `OAUTH_MERGE_ACCOUNTS_BY_EMAIL=true`. A gateway records these when it starts, so restart it after changing them |
| A delegate's grant is refused as outside their access | They must hold that capability in that agent themselves. An org admin can grant it |
| A confirmation link says the request isn't available | Only the person who proposed it can open it. It expires after `HUBZOID_CHANGE_REQUEST_TTL` seconds and works once |
| Workflow absent or in error | `hubzoid doctor`, the workflow's state and error, a valid schedule and timezone, bridge logs |
| No execution history | The correct hub's DBOS database, workflow enabled, run actually submitted |
| User still has access after revocation | "Everyone signed in" or inherited access. Use **Delete user** to offboard |
| `hubzoid grant '*' ...` is refused | New access for everyone signed in can't be created. Grant named people |
