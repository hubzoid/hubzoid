# Access management

Hubzoid owns accounts, sign-in, and access to agents and restricted tools.
Administrators manage them in the web app's **Settings** (Team & deployment),
with the same session they chat with. Permissions are enforced outside the
model.

Every decision about who may change access, create accounts or confirm a change
goes through one service (`hubzoid/access/service.py`). Settings, the
management API and the agent tools all call it. None of them decides authority
on its own.

## Accounts and grants

Public sign-up is closed by default. Managers add people with **Add user**, on
an agent's **Access** tab or on **People**. **Add user** always creates a new
user: their sign-in and their first access in one step. To change what an
existing user can do, edit their access instead (below). Add user never changes
an existing account.

### Add a user (implemented)

1. Open **Agents → the agent → Access → Add user**, or **People & access →
   Add user**.
2. Enter their name and email. They sign in with a one-time link, or choose
   **Google sign-in only** when the deployment supports it (below); otherwise
   that option is disabled with a short hint.
3. Choose their initial access. From an agent, that agent's capabilities are
   shown directly. From People, each agent you manage is a collapsed section.
   Inside, optional groups start collapsed and show how many are selected.
   Anything you may not give is shown as **Outside your access** or **Admins
   only** and cannot be selected. Descriptions are in tooltips.
4. **Review**, then **Create account**.
5. The one-time sign-in link is shown once, with **Copy**. Share it yourself:
   no invitation or email is sent. The person opens it, sets a password and is
   signed in. It lasts 72 hours and works once ([authentication](auth.md#passwords)).

The account is created with the normal `user` role. Hubzoid then binds it to
its email and applies the grants, audited as `account_create` plus each grant.
No password is chosen or seen by the manager. Initial access is per agent. The
Administrator role is set from the person's details once the account exists.

What happens when something goes wrong:

- **The email already has an account.** Nothing is created or changed. Settings
  says the user already exists and links to them (**Edit this user**, or **Edit
  their access** on an agent). There is no way to grant from this message:
  edit their access, review it, and save.
- **The account was created but access could not be saved.** Hubzoid keeps the
  account, records it without access, and says exactly that. **Try again**
  grants the access to that account. It is never created twice, and nothing is
  deleted to look like a rollback.
- **The email belonged to a deleted account.** Only an organization
  administrator can re-create it. The old grants are removed first, audited as
  `account_replaced`.

Add user no longer offers to pre-approve an email that has no account. A grant
to an email that starts working when someone first signs in with it (for
example through single sign-on) can still be made with `hubzoid grant <email>
use_hub --hub <hub>` or the management API.

### Edit an existing user's access (implemented)

- On an agent: **Access → Edit access** on their row.
- From **People**: open the user. **Access by agent** lists each agent they can
  use, with **Edit access**. **Add an agent** opens the editor for an agent
  they can't use yet, starting with Use this agent.

Either way you review the change before it is saved. A blocked user is shown as
blocked, and new access for them is refused before saving.

### Google sign-in only (implemented)

**Google sign-in only** can be selected only when Google sign-in attaches to an
existing account by its verified email. That needs:

- `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` set
- `OAUTH_MERGE_ACCOUNTS_BY_EMAIL=true`

The account has no password, so nobody can sign in with one. The person
selects **Sign in with Google** and the sign-in attaches to the account by
email. When `OAUTH_ALLOWED_DOMAINS` limits Google sign-in, Settings accepts
only those domains. The user's details then say their sign-in is managed
through Google, and Settings does not reset it. Adding a user never changes
sign-in policy: merging, sign-up and domains stay operator settings (see
[authentication](auth.md)).

A gateway records these facts in its deployment manifest as flags, for example
`"sign_in": {"google": true, "merge_by_email": true}` plus any allowed domains,
never a client id or secret. Bridges started separately (`gateway
--no-bridges`) read them from there. The gateway rewrites them on each start. A
standalone `hubzoid run` reads its own environment.

A password account can also sign in with Google once the deployment is set up
this way.

### User details (implemented)

Open a user from **People**. Their details show name, email, status, **Role**
and **Access by agent**. For organization administrators they also show:

- **Role: User or Administrator.** One role, with a confirmation. See below.
- **Approve**, in place of the role, for a signup awaiting approval.
- **Reset password.** A one-time link is shown once to share. Using it ends
  the person's other sessions. For a Google-only user the details say the
  sign-in is managed through Google instead.
- **Delete user**, in the **…** menu. See below.

You cannot change your own role or account here. Changing an account's email is not offered: grants are keyed on the
email, so create a new account instead.

### One Administrator role (implemented)

**Administrator** is organization administration (Manage access for the whole
deployment) together with the account's `admin` role. **User** clears both.
They are always set together:

- The account's role is set first, then organization administration. If only
  one side changes, Settings says which side is set and offers **Try again**,
  which finishes the change. The API answers `502 role_partial`, and repeating
  the same request finishes it.
- A user who is an administrator on only one side (for example, from an older
  release or `hubzoid admin set-role`) shows **Needs attention**. Reading their
  details never promotes or demotes anyone. Choose a role to fix it.
- The last administrator can't be made a User or deleted, on either side.
- Changes are audited as `account_role` plus the grant or revoke.

Delegates (Manage access on specific agents) have no role control.

### Delete a user (implemented)

**…** → **Delete user**, then type their email to confirm. This is a real
deletion:

- Every grant is removed, then the account is deleted with its chats, shared
  chat links and connections.
- Kept: Activity history, usage records and artifacts they published (their
  public links stop working). The email is marked as removed, so it inherits
  nothing; only an organization administrator can create an account with it
  again.
- The last administrator can't be deleted.
- Audited as `account_delete`.

### Blocked users

Settings doesn't offer Block. Someone can still be blocked: a block from an
earlier release, or one carried over by `hubzoid migrate`. They show as **Blocked**,
and new access for them is refused. An organization administrator opens their
details and chooses **Reactivate** in the "…" menu (or calls
`POST /portal/api/people/block` with `{"subject": "<email>", "suspended":
false}`), or deletes them. Reactivating does not restore the grants the block
removed.

The capability picker shows names and compact Inherited, Required or Locked
labels. Question-mark buttons expose descriptions and restriction details on
hover, keyboard focus or tap. Review and confirm before changes are saved.

## Agent capabilities

| Capability | What it allows |
|---|---|
| Use this agent (`use_hub`) | Enter the hub through supported authenticated surfaces and use unrestricted tools |
| A restricted module, such as `erp` | Call that module's tools; agent entry is included |
| Manage access (`manage_access`) | Review/change access; a direct agent grant includes basic chat, but not restricted tools. Organization-wide admin rights alone do not grant chat |
| See workflows and runs (`workflows_view`) | List this agent's workflows and recent runs from chat or an assistant ([workflows.md](workflows.md#from-chat-and-assistants)) |
| Run and control workflows (`workflows_manage`, sensitive) | Start, pause, resume and cancel this agent's workflows from chat or an assistant. Runs act as the workflow's own account |
| Schedule reports for themselves (`schedule_reports`) | Save personal schedules of the reports they can run, from chat ([workflows.md](workflows.md#reports-people-run-and-schedule-in-chat-13)) |
| Share reports with the company domain (`share_domain_links`, sensitive) | Give a report a company link anyone at the share domains can open after signing in ([reports-and-email.md](reports-and-email.md#company-links)) |
| Run *report* (`wf_<workflow>`) | One per workflow offered in chat (`as_tool=True`): run it for themselves, as themselves |
| Manage access from chat (`access_tools`, sensitive, admins only) | Use the access tools below in this agent's chat. Has an effect only for people who manage access |

An organization administrator manages access across the deployment. That is not
a blanket grant to use every agent or every restricted tool. People with no entry
permission do not see a managed hub in the chat picker, including administrators.
The bridge checks access again at execution.

## Who sees the team sections of Settings

| Account access | Team & deployment sections |
|---|---|
| Organization-wide `manage_access` | All registered agents, organization administration and account actions |
| `manage_access` on specific agents | Only those agents, within the delegate ceiling below. Can create a normal account with access in those agents |
| Chat or restricted-tool access only | None: their own sections only |

Opening a team section's address directly does not bypass this: its API
(`/portal/api`) verifies the signed-in account and administrator scope.

## Delegated management and its ceiling (implemented)

A delegate is someone with `manage_access` on specific agents. In each agent
they manage, a delegate may grant or remove only capabilities they currently
hold there themselves, minus `manage_access`. This ceiling is checked by the
server on every write and again when a proposed change is confirmed. Settings
greys out anything outside it, but the server is the check.

A delegate cannot:

- grant `manage_access` (no sub-delegation)
- change their own access
- remove access for everyone signed in (nobody can create it), or change an
  organization administrator's access
- remove all of someone's access in an agent when that person holds a
  capability the delegate does not
- reset passwords, approve, change roles or delete accounts

A delegate can create a normal account, but only with at least one grant in an
agent they manage and within their ceiling, never an administrator. **Try
again** after a partial create is checked the same way. Organization
administrators keep their full scope.

## Management API (implemented)

The API behind the team sections, under `/portal/api`, is also the management
API. It accepts the web app's session cookie; every write needs a same-site
Origin header. The Open WebUI API keys that 1.x accepted here are no longer
read. Endpoints and response fields are listed in
[PORTAL-ACCOUNT-CONTRACT.md](PORTAL-ACCOUNT-CONTRACT.md).

## Access tools in chat, WhatsApp and MCP (implemented, off until granted)

An agent can help managers see access and propose changes. An organization
administrator turns this on per person: **Agents → the agent → Access**, then
**Hubzoid tools → Access control → Manage access from chat** (`access_tools`).
Delegates can't grant it. It has an effect only for people who manage access,
and what they can see or propose stays within what they manage.

| Tool | What it does |
|---|---|
| `my_management_scope` | The agents you manage and what you can grant in each |
| `who_has_access` | People with access to one agent you manage, their capabilities and account state |
| `explain_access` | One person's effective access in the agents you manage, and why: a direct grant, everyone signed in, or organization administration |
| `propose_access_change` | Propose granting or removing capabilities for one person in one agent |
| `propose_new_account` | Propose a chat account for a new person with access to one agent |

They work only on an agent whose access is managed in Settings.

- The acting person is always the signed-in caller. No tool takes an actor.
- They run on `web`, `api`, `mcp`, `whatsapp` and `telegram` (and `owui`,
  the surface name kept for conversations moved from Open WebUI). They
  refuse anonymous callers, scheduled workflows and every Slack surface.
- They only propose. Nothing changes until the same person opens the link
  (`/settings#/confirm/<id>`), signs in on the web and confirms the exact change.
- A proposal expires after `HUBZOID_CHANGE_REQUEST_TTL` seconds (default 900),
  can be used once, and is checked again against the person's current access
  when confirmed. At most 20 can wait per person.
- A proposed account has no password. The manager sets it on the confirmation
  page, so it never enters the model's context.
- `change_proposed`, `change_confirmed`, `change_rejected`, `change_expired` and
  `change_failed` are audited with the surface and request id. The grants made
  by a confirmed request carry the same request id.
- `HUBZOID_ACCESS_TOOLS=false` in a hub's `.env` removes the tools from that
  agent, and Settings shows the capability as disabled.
- Upgrading from 1.0.x: `HUBZOID_MANAGEMENT_TOOLS=true` keeps its old meaning
  (every manager gets the tools without a grant) for one more release, with a
  warning in the log and in `hubzoid doctor`. Grant `access_tools` to the
  managers who should keep them, then remove the setting.
  `HUBZOID_MANAGEMENT_TOOLS=false` still turns the tools off.

## First owner

A newly initialized local hub provisions the verified `admin@localhost` account
once. A shared deployment uses the administrator account matching
`HUBZOID_ADMIN_EMAIL`. Configure it before the first sign-in. No ordinary user or arbitrary admin is promoted automatically.

On an unbootstrapped deployment the owner receives organization administration,
with entry provisioned once per configured hub. An existing administration
bootstrap is preserved. Adding a hub never restores a revoked organization role.
Removing access later is intentional and
is not undone on the next sign-in.

See [administration](ADMINISTRATION.md) for recovery and migration commands.

## Grant a teammate access

1. If they don't have an account yet: **Agents → the agent → Access → Add
   user**, create it with the capabilities they need, and share the sign-in
   details.
2. If they already have one: **Edit access** on their row, or open them under
   **People** and use **Add an agent**. Choose capabilities, review, and save.
3. Share the chat URL. No invitation is sent. A person awaiting approval is
   approved from their details under People.
4. Verify with that person's account that allowed agents appear and a disallowed
   tool stays unavailable.

Changes apply atomically. If someone else edited access first, reload and review
again. A lost response can leave the save uncertain; refresh before retrying.
**People** distinguishes a blocked user from a pending or unavailable account.

## Everyone signed in (no longer granted)

New agents need named grants to sign-in accounts (Add user). Nobody
can create access for "everyone signed in" (the `*` subject), organization
administrators included. Settings has no control for it, and the service,
the management API, change proposals, `hubzoid grant` and the grant store all
refuse it.

An agent that already had one keeps it, and it keeps working:

- It exists only on agents that had it before 1.1. Nothing creates a new one.
- The agent's Access list shows an **Everyone signed in** row with Use this
  agent. It cannot be edited, only removed.
- Only an organization administrator can remove it. Delegates see it read-only.

To replace it with named grants:

1. Open **Agents → the agent → Access**. The notice above the list says the
   agent is open to everyone signed in.
2. Give the people who need the agent named access: **Add user** for someone
   new, or **People → the user → Add an agent** for an existing user.
   Workflows run as an ordinary account: give that account access the same
   way.
3. Select **Remove** on the **Everyone signed in** row. The confirmation says how
   many users open the agent only through it. They, and anyone who signs
   up later, lose entry. Named grants are not affected.

From a shell: `hubzoid revoke '*' use_hub --hub <hub> <hub_dir>`.

## Capabilities in Settings

**Agents → the agent → Access → Edit access** lists what a person or service may
do, in groups. Empty groups are hidden.

| Group | What it holds |
|---|---|
| Hub access | Use this agent (`use_hub`) |
| Restricted tools | The agent's own `restricted/<capability>.py` modules |
| Connectors | One row per connector this agent offers (`connector_<id>`): **Connect <name>** for a connector each person signs in to, **Use <name>** for a Shared key connector |
| Workflows | Workflow-only capabilities (none today; the workflow tools live under Hubzoid tools) |
| Administration | Manage access (`manage_access`): change access to this agent and create chat accounts for it, within your own access |
| Hubzoid tools | Built-in tools that register a capability, such as Save shared knowledge (`curator`). Sections inside it: **Workflows** (`workflows_view`, `workflows_manage`, `schedule_reports`, `share_domain_links` and one `wf_<workflow>` per report) and **Access control** (`access_tools`) |
| No longer available | A grant whose capability no longer exists. Remove it; it can't be granted again |

Availability and permission are separate. A short status next to a capability
says whether its settings are present: for example "Jev key missing", or
"Not checked" when they may be in an AWS secret Settings does not read.
Settings checks setting names only, never values, and "configured" means
present, not verified. You can grant a capability before it is configured. It
cannot run until an operator adds the setting. Adding a setting never grants
anyone access.

An **Included** capability comes with Use this agent and has nothing to grant.
**Required**, **Inherited** and **Outside your access** mean what they say; the
help button on each row explains why.

## Restrict a tool

Put the tool factory in `restricted/<capability>.py`. All tools returned by that
module share its capability name. Keep credentials in `restricted/.env` or your
runtime's secret injection. Do not return secrets from a tool or workflow step.

Optional `identity/permissions.yaml` gives these capabilities useful labels,
descriptions and sensitivity indicators. Fields are optional; no new required
hub file is introduced. It applies to restricted capabilities only: an entry
naming a built-in (`use_hub`, `manage_access`, `curator` and so on) is ignored
with a warning. Settings discovers capability names without executing
the restricted Python modules.

## Registering a capability (for Hubzoid contributors)

A built-in tool appears in Settings by registering one `Capability` in the
module that enforces it (`hubzoid/capabilities.py`). No Settings code is needed.

```python
from hubzoid.capabilities import Capability, register

JEV = register(Capability(
    permission="jev",                  # stable id; grants refer to it; never rename
    label="Call Jev",
    group="tools",                     # hub | tools | restricted | workflows | admin
    description="Use call_jev in chat for typed decisions from Jev. "
                "Each call is billed to the hub's JEV_OPENROUTER_API_KEY.",
    surfaces=("chat",),                # implemented surfaces only: chat, mcp, workflow
    requires=("JEV_OPENROUTER_API_KEY",),  # setting names, checked locally
    missing="Jev key missing",         # short status when a setting is absent
))

# ...where the tool is added to the registry, gate it with the same id:
registry[ft.name] = access.guard_tool(ft, JEV.permission, hub_dir)
```

Then add the module to `capabilities.REGISTRANTS`.

- `default="grant"` (the default) means off until granted. `default="included"`
  means it comes with Use this agent and has no grant; use it only for rows that
  inform, such as an Email me setting status. A sensitive capability cannot be
  included.
- `sensitive=True` marks it for review when granted.
  `delegate_grantable=False` lets only organization administrators grant it. The
  service enforces both; Settings only explains them.
- `enabled_by="SOME_SWITCH"` (or a tuple of names): when a named setting is
  present and false, the status is "Disabled for this hub". The tool module
  checks `capabilities.switched_off(CAP)` to leave its tools out.
- `section="workflows"` or `"access"` shows the row under that sub-heading
  inside its group. Rows without a section come first.
- `probe=fn` (`fn(hub_dir) -> str`) reports why the capability can't run in a
  hub, for example "No workflows in this agent". It runs only when the
  settings allow the capability, and a failing probe reads "Not checked".
- Keep `guard_tool` as the only gate: it hides the tool from callers without the
  grant and refuses a call that reaches it anyway, on every runtime.
- Workflow APIs are not chat exposure. A workflow's `hub.call_jev` stays
  available when the chat tool is not granted.

A second example, a capability used outside chat:

```python
SHARE_PUBLIC = register(Capability(
    permission="share_public_links", label="Share artifacts publicly", group="tools",
    description="Create 'anyone with the link' links for reports you own. Anyone who "
                "has such a link can open the report without signing in.",
    surfaces=(), sensitive=True,
))
```

Publishing within an authorized workflow run and Email me need no grant. The
artifact Share dialog decides one artifact's audience; Settings only grants
capabilities such as creating public links.

A scheduled workflow or Markdown task acts as an ordinary account: its
`run_as`, else `HUBZOID_WORKFLOW_USER`, else the owner recorded at setup (see
[workflow-identity.md](workflow-identity.md)). Grant that account the tools the
work needs, like any person. Teams that want shared automation create an
ordinary account for it. Grants to the older `workflow:<function>` and
`workflow:md:<task>` subjects are kept but no longer used by runs; a run whose
account lacks a permission the old subject held logs which one.

The built-in **Save shared knowledge** capability (`curator`) controls
`remember`. Grant it in Settings to the accounts (people, or the account a workflow runs as) that should
create or replace documents under `knowledge/_learned/`. These documents are
shared agent knowledge, not private conversation memory. No `restricted/curator.py`
file is required to make this capability appear.

## Decisions with Jev in chat

The built-in **Call Jev** capability (`jev`) controls the
`call_jev` chat tool. It asks Jev typed `noul`, `choice` and `score` questions,
the same as a workflow's `hub.call_jev` ([workflows.md](workflows.md#decisions-with-jev)).
Nobody has it by default. Agent entry (`use_hub`) and **Manage access** do not
include it. Grant it only to people who need it, because each call is billed to
the hub's `JEV_OPENROUTER_API_KEY`. Without that key a granted call fails with
a message that names it.

Each call writes a usage row (kind `jev`) naming the person, their channel and
the chat, and an access log entry.

Where Jev is available today:

| Surface | Jev |
|---|---|
| Code workflows (`hub.call_jev`) | Yes. Workflow code needs the key, not a grant |
| Chat in the web app, and the bridge API behind it | With the `jev` grant |
| Agent runs inside workflows (`hub.call_agent`, markdown tasks) | Only when the account the run acts as holds `jev` (see [workflow-identity.md](workflow-identity.md)) |
| Slack, WhatsApp and Telegram | No. They cannot reach controlled tools until those channels verify each person's identity |
| Hosted MCP (`/mcp`) | No. `call_jev` is not exposed to external assistants |

## Who sees a controlled tool

A controlled tool (a `restricted/` module, `remember` or `call_jev`) is left out
of the tools the agent is shown for anyone who may not use it, on every runtime
(`claude-local`, `codex-local` and LiteLLM models). A call that names it anyway
is answered as an unknown tool, and the guard also refuses and logs any call
that reaches it another way.

## One access model

Every agent's access is managed here. A person may use an agent, or one of its controlled tools,
only with a grant. There is no other source:

- Groups (including those moved from Open WebUI) and roster groups
  (`identity/access.csv`) grant nothing. A roster still says who a WhatsApp or Telegram sender is.
- A hub deployed from git, with no `.hubzoid/` folder, is managed like any
  other. Nobody can use it until someone is granted access.
- The configured owner gets the owner's grants on their first verified sign-in
  as an administrator (the web app or MCP sign-in). With sign-in off, the local owner owns every hub from the first
  start. An API key never provisions anyone.
- A call to the bridge without a verified person is refused, as on any managed
  agent.
- `hubzoid doctor` warns about an agent nobody may use and notes one only its
  owner may use (`access.who`).

## Planned, not built yet

- Settings does not yet list another person's connections. People see their
  own in Settings → My connections; see [mcp.md](mcp.md).

## Administration boundary

Configure hub workflows in `schedule/` or `workflows/`, and inspect them in
Settings. Open WebUI functions and pipes moved by the migration are kept
switched off and never run.

Agent prompts are not an authorization boundary. Do not expose the bridge port
directly, trust browser-supplied identity headers, or enable development identity
overrides on a shared deployment. See [DEPLOYING.md](DEPLOYING.md).
