# Access management

Open WebUI owns credentials and sign-in. Hubzoid owns access to agents and
restricted tools, and manages accounts through Open WebUI's admin API.
Administrators use the **Admin Console** with the same verified chat session.
Permissions are enforced outside the model.

Every decision about who may change access, create accounts or confirm a change
goes through one service (`hubzoid/access/service.py`). The Console, the
management API and the agent tools all call it. None of them decides authority
on its own.

## Accounts and grants

Public sign-up is closed by default. Managers add people with **Add user**, on
an agent's **Access** tab or on **People**. **Add user** always creates a new
user: their sign-in and their first access in one step. To change what an
existing user can do, edit their access instead (below). Add user never changes
an existing account.

### Add a user (implemented)

1. Open **Agents → the agent → Access → Add user**, or **People → Add user**.
2. Enter their name and email. The sign-in is a password: type one or use
   **Generate**. **Google sign-in only** sits beside the password. It is
   selectable only when the chat app supports it (below); otherwise it is
   disabled with a short hint.
3. Choose their initial access. From an agent, that agent's capabilities are
   shown directly. From People, each agent you manage is a collapsed section.
   Inside, optional groups start collapsed and show how many are selected.
   Anything you may not give is shown as **Outside your access** or **Admins
   only** and cannot be selected. Descriptions are in tooltips.
4. **Review**, then **Create account**.
5. For a password account, the sign-in details (chat address, email and
   password) are shown once, with **Copy** and **Copy sign-in details**. Share
   them yourself. No invitation or email is sent. The password is dropped from
   the page when you select **Done**.

The account is created with Open WebUI's normal `user` role through its admin
API (`POST /api/v1/auths/add`), as the deployment's service account on the
internal URL. Hubzoid then binds the new account to its email and applies the
grants in one transaction, audited as `account_create` plus each grant. The
password is never stored, logged, audited or returned by the server. Initial
access is per agent. The Administrator role is set from the person's details
once the account exists.

What happens when something goes wrong:

- **The email already has an account.** Nothing is created or changed. The
  Console says the user already exists and links to them (**Edit this user**,
  or **Edit their access** on an agent). There is no way to grant from this
  message: edit their access, review it, and save.
- **The account was created but access could not be saved.** The chat app and
  the access store cannot commit together. Hubzoid keeps the account, records
  it without access, and says exactly that. The sign-in details stay on screen.
  **Try again** grants the access to that account. It is never created twice,
  and nothing is deleted to look like a rollback.
- **The chat app did not answer.** The Console says it couldn't confirm the
  result. **Try again** is safe: if the account was made, the chat app refuses
  a duplicate and the Console says the user exists, noting that the account may
  use the password you set in the earlier attempt.
- **The email belonged to a deleted account.** Only an organization
  administrator can re-create it. The old grants are removed first, audited as
  `account_replaced`.
- **Account management isn't set up.** The Console needs the chat app's
  internal URL and `HUBZOID_GATEWAY_ADMIN_EMAIL`/`HUBZOID_GATEWAY_ADMIN_PASSWORD`.
  It refuses to write through a public URL. Add user says so until then.

Console account creation works on a deployment whose agents still use legacy
access. Access for those agents keeps coming from Open WebUI groups.

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

**Google sign-in only** can be selected only when the chat app attaches a
Google sign-in to an existing account by email. That needs all of these for the
chat app:

- `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` set
- `OAUTH_MERGE_ACCOUNTS_BY_EMAIL=true`
- OAuth settings from the environment (`ENABLE_OAUTH_PERSISTENT_CONFIG` not
  `true`), so what Hubzoid reads is what the chat app uses

The account is created through the same admin call with a random password
generated on the server. It is never returned, shown, stored or logged, so
nobody can sign in with a password. The person selects **Sign in with Google**
and Open WebUI links the sign-in to the account by email. When
`OAUTH_ALLOWED_DOMAINS` limits Google sign-in, the Console accepts only those
domains. The user's details then say their password is managed through Google,
and the Console does not reset it.

Only Google is offered. Open WebUI 0.11.4 links by email without checking the
provider's `email_verified` claim, and Google account emails are verified.
Generic OIDC and Microsoft are not offered. Adding a user never changes sign-in
policy: merging, sign-up and domains stay operator settings (see
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
- **Reset password.** The new password is shown once. Open WebUI signs the
  person out of other sessions. For a Google-only user the details say the
  password is managed through Google instead.
- **Delete user**, in the **…** menu. See below.

You cannot change your own role or account here, nor the Console's service
account. Changing an account's email is not offered: grants are keyed on the
email, so create a new account instead.

### One Administrator role (implemented)

**Administrator** is organization administration in Hubzoid (Manage access for
the whole deployment) together with the chat app's `admin` role. **User**
clears both. They are always set together:

- The chat app's role is set first, then Hubzoid administration. If only one
  side changes, the Console says which side is set and offers **Try again**,
  which finishes the change. The API answers `502 role_partial`, and repeating
  the same request finishes it.
- If the chat app refuses clearly, nothing is changed. If its answer is
  unclear, Hubzoid administration is left as it was.
- A user who is an administrator on only one side (for example, made one in
  the chat app directly) shows **Needs attention**. Reading their details never
  promotes or demotes anyone. Choose a role to fix it.
- The last administrator can't be made a User or deleted, on either side. The
  Console's service account counts as a chat-app administrator only when it
  also holds Hubzoid administration, that is, when someone administers with it.
- Changes are audited as `account_role` plus the grant or revoke.

Delegates (Manage access on specific agents) have no role control.

### Delete a user (implemented)

**…** → **Delete user**, then type their email to confirm. This is a real
deletion:

- Every grant is removed, then the chat account is deleted through Open WebUI,
  which also deletes their chats, shared chat links and group memberships.
- Kept: Activity history, usage records and artifacts they published (their
  public links stop working). The email is marked as removed, so it inherits
  nothing; only an organization administrator can create an account with it
  again.
- The last administrator can't be deleted.
- Audited as `account_delete`.

### Blocked users

The Console no longer offers Block or Reactivate. A user blocked in an earlier
release stays blocked: they show as **Blocked**, and new access for them is
refused. Delete them, or unblock them through the management API
(`POST /portal/api/people/block` with `{"subject": "<email>", "suspended":
false}`, as an organization administrator). Unblocking does not restore the
grants the block removed.

The capability picker shows names and compact Inherited, Required or Locked
labels. Question-mark buttons expose descriptions and restriction details on
hover, keyboard focus or tap. Review and confirm before changes are saved.

## Agent capabilities

| Capability | What it allows |
|---|---|
| Use this agent (`use_hub`) | Enter the hub through supported authenticated surfaces and use unrestricted tools |
| A restricted module, such as `erp` | Call that module's tools; agent entry is included |
| Manage access (`manage_access`) | Review/change access; a direct agent grant includes basic chat, but not restricted tools. Organization-wide admin rights alone do not grant chat |

An organization administrator manages access across the deployment. That is not
a blanket grant to use every agent or every restricted tool. People with no entry
permission do not see a managed hub in the chat picker, including chat-app admins.
The bridge checks access again at execution.

## Who can open the Admin Console

| Account access | Console visibility |
|---|---|
| Organization-wide `manage_access` | All registered agents, organization administration and account actions |
| `manage_access` on specific agents | Only those agents, within the delegate ceiling below. Can create a normal account with access in those agents |
| Chat or restricted-tool access only | No Console access |
| Open WebUI admin role only | No automatic Console access; the designated first owner is bootstrapped as described below |

The chat sidebar link follows these permissions. Opening `/portal/` directly does
not bypass them: its API verifies the signed-in account and administrator scope.

## Delegated management and its ceiling (implemented)

A delegate is someone with `manage_access` on specific agents. In each agent
they manage, a delegate may grant or remove only capabilities they currently
hold there themselves, minus `manage_access`. This ceiling is checked by the
server on every write and again when a proposed change is confirmed. The
Console greys out anything outside it, but the server is the check.

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

The Console's API under `/portal/api` is also the management API. Besides the
session cookie, in the legacy Open WebUI mode (`HUBZOID_UI=openwebui`) only,
every endpoint accepts `Authorization: Bearer sk-...` with the caller's Open
WebUI API key. The Hubzoid web app mode ignores these keys. The key is verified against Open WebUI's key table,
as for the hosted MCP surface, and the caller acts as its owner under the same
rules. A key caller does not need an Origin header. Cookie callers do, for every
write. Keys can only be minted where Open WebUI API keys are enabled (Hubzoid
turns them on with `MCP_SERVER=true`). Endpoints and response fields are listed
in [PORTAL-ACCOUNT-CONTRACT.md](PORTAL-ACCOUNT-CONTRACT.md).

## Proposals from chat, WhatsApp and MCP (implemented, off by default)

Set `HUBZOID_MANAGEMENT_TOOLS=true` in a hub's `.env` to give its agent three
tools: `my_management_scope`, `propose_access_change` and `propose_new_account`.
They work only on an agent whose access is managed in the Console.

- The acting person is always the signed-in caller. No tool takes an actor.
- They run on `owui`, `web`, `api`, `mcp`, `whatsapp` and `telegram`. They
  refuse anonymous callers, scheduled workflows and every Slack surface.
- They only propose. Nothing changes until the same person opens the link
  (`/portal/#/confirm/<id>`), signs in on the web and confirms the exact change.
- A proposal expires after `HUBZOID_CHANGE_REQUEST_TTL` seconds (default 900),
  can be used once, and is checked again against the person's current access
  when confirmed. At most 20 can wait per person.
- A proposed account has no password. The manager sets it on the confirmation
  page, so it never enters the model's context.
- `change_proposed`, `change_confirmed`, `change_rejected`, `change_expired` and
  `change_failed` are audited with the surface and request id. The grants made
  by a confirmed request carry the same request id.

## First owner

A newly initialized local hub provisions the verified `admin@localhost` account
once. A shared deployment uses the designated existing Open WebUI administrator
matching `WEBUI_ADMIN_EMAIL` or `HUBZOID_GATEWAY_ADMIN_EMAIL`. Configure it before
the first sign-in. No ordinary user or arbitrary admin is promoted automatically.

On an unbootstrapped deployment the owner receives organization administration,
with entry provisioned once per configured hub. An existing administration
bootstrap is preserved. Adding a hub never restores a revoked organization role.
Removing access later is intentional and
is not undone on the next sign-in. Fresh hubs become authoritative; existing hubs
keep their prior access mode until explicitly migrated.

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
administrators included. The Console has no control for it, and the service,
the management API, change proposals, `hubzoid grant` and the grant store all
refuse it.

An agent that already had one keeps it, and it keeps working:

- It is carried over only by `hubzoid access migrate` when legacy access was
  demonstrably public (an Open WebUI model open to all users, or
  `--standalone-public`). The migration report says
  `Everyone signed in (carried over)`.
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

## Capabilities in the Console

**Agents → the agent → Access → Edit access** lists what a person or service may
do, in groups. Empty groups are hidden.

| Group | What it holds |
|---|---|
| Hub access | Use this agent (`use_hub`) |
| Hubzoid tools | Built-in tools that register a capability, such as Save shared knowledge (`curator`), and connector capabilities |
| Custom restricted tools | `restricted/<capability>.py` modules |
| Workflows | Workflow-only capabilities (none today) |
| Administration | Manage access (`manage_access`): change access to this agent and create chat accounts for it, within your own access |
| No longer available | A grant whose capability no longer exists. Remove it; it can't be granted again |

Availability and permission are separate. A short status next to a capability
says whether its settings are present: for example "Jev key missing", or
"Not checked" when they may be in an AWS secret the Console does not read.
The Console checks setting names only, never values, and "configured" means
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
with a warning. The Console discovers capability names without executing
the restricted Python modules.

## Registering a capability (for Hubzoid contributors)

A built-in tool appears in the Console by registering one `Capability` in the
module that enforces it (`hubzoid/capabilities.py`). No Console code is needed.

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
  service enforces both; the Console only explains them.
- `enabled_by="SOME_SWITCH"`: when that setting is present and false, the status
  is "Disabled for this hub".
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
artifact Share dialog decides one artifact's audience; the Console only grants
capabilities such as creating public links.

A scheduled workflow or Markdown task acts as an ordinary account: its
`run_as`, else `HUBZOID_WORKFLOW_USER`, else the owner recorded at setup (see
[workflow-identity.md](workflow-identity.md)). Grant that account the tools the
work needs, like any person. Teams that want shared automation create an
ordinary account for it. Grants to the older `workflow:<function>` and
`workflow:md:<task>` subjects are kept but no longer used by runs; a run whose
account lacks a permission the old subject held logs which one.

The built-in **Save shared knowledge** capability (`curator`) controls
`remember`. Grant it in Console to the accounts (people, or the account a workflow runs as) that should
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
the chat, and an access log entry. On a hub that has not been migrated, an Open
WebUI group named `jev` grants it instead.

Where Jev is available today:

| Surface | Jev |
|---|---|
| Code workflows (`hub.call_jev`) | Yes. Workflow code needs the key, not a grant |
| Chat in Open WebUI, and the bridge API behind it | With the `jev` grant |
| Agent runs inside workflows (`hub.call_agent`, markdown tasks) | Only when the account the run acts as holds `jev` (see [workflow-identity.md](workflow-identity.md)) |
| Slack, WhatsApp and Telegram | No. They cannot reach controlled tools until those channels verify each person's identity |
| Hosted MCP (`/mcp`) | No. `call_jev` is not exposed to external assistants |

## Who sees a controlled tool

A controlled tool (a `restricted/` module, `remember` or `call_jev`) is left out
of the tools the agent is shown for anyone who may not use it, on every runtime
(`claude-local`, `codex-local` and LiteLLM models). A call that names it anyway
is answered as an unknown tool, and the guard also refuses and logs any call
that reaches it another way.

## Existing hubs

Unmigrated hubs retain their legacy group/roster rules. The Console labels this
mode and does not pretend its draft grants have replaced the old authority.
Follow the backup, dry-run and activation procedure in [ADMINISTRATION.md](ADMINISTRATION.md).
The older mechanics remain documented in [legacy-access.md](legacy-access.md)
for migration and diagnosis only. Do not use that guide to configure a new
managed hub.

## Hiding the Open WebUI Users page (implemented)

On by default for a gateway set up fresh with Console accounts (recorded as
`hide_owui_users` in `deployment.json`); off for existing deployments. An
explicit `HUBZOID_HIDE_OWUI_USERS=true|false` in the environment of the process
that runs the edge (the gateway, or `hubzoid run`) wins either way. When hidden:

- Opening Open WebUI's user list (`/admin/users/overview`) lands on Console
  **People**.
- When every agent in the deployment is managed in the Console, Open WebUI's
  groups decide nothing, so its whole Users section is hidden: the Admin Panel
  (`/admin`) and every `/admin/users` page, Groups included, open **Settings →
  Integrations** before anything renders, and the Admin Panel shows no Users
  link. This is checked on each request, so it follows a hub's migration.
- While any agent still uses legacy access, the Users section opens Groups,
  and Groups stays.
- Settings, Evaluations and Functions are unchanged.
- Browser writes to Open WebUI's account admin API get 403: `POST
  /api/v1/auths/add`, `POST /api/v1/users/{id}/update` and `DELETE
  /api/v1/users/{id}`. A person's own settings (`/api/v1/users/user/...`) are
  unaffected.
- Hubzoid's own calls go to the internal URL and never pass the edge.

## Planned, not built yet

- The Console does not yet list a person's connections. Connections started
  from chat (`connector_<app>` capabilities and the connection journey) are
  built and off unless `HUBZOID_CONNECT_JOURNEY` is set; see [mcp.md](mcp.md).

## Administration boundary

Open WebUI's account, sign-in, connection and upstream administration controls
remain available. Its functions and automations are Open WebUI features; they do
not edit Hubzoid's hub-folder workflows or runtime configuration. Configure hub
workflows in `schedule/` or `workflows/`, and inspect them in the Console.

Agent prompts are not an authorization boundary. Do not expose the bridge port
directly, trust browser-supplied identity headers, or enable development identity
overrides on a shared deployment. See [DEPLOYING.md](DEPLOYING.md).
