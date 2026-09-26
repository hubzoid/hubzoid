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

Public sign-up is closed by default. A manager creates a login in **People → Add
account** (see below) or an administrator creates one in Open WebUI. **Add person**
on an agent's Access tab grants permissions to an email address. It does not
create an account or send an invitation. Access can be prepared before the
matching account exists.

### Create an account in the Console (implemented)

1. Open **People → Add account**.
2. Enter the email address, the person's name and a password. Type one or use
   **Generate**.
3. Tick initial access in the agents you manage. Capabilities you may not give
   are shown as **Outside your access** and cannot be selected.
4. Review, then **Create account**.
5. The password is shown once with **Copy**. Share it with the person directly.

The account is created with Open WebUI's normal `user` role through its admin
API (`POST /api/v1/auths/add`), as the deployment's service account on the
internal URL. Hubzoid then binds the new account to its email and applies the
grants in one transaction, audited as `account_create` plus each grant. The
password is never stored, logged, audited or returned by the server.

What happens when something goes wrong:

- **The email already has an account.** Nothing is created. The Console offers
  **Grant access** on the agent instead.
- **The account was created but access could not be granted.** Hubzoid deletes
  the new account (it has no chats) and says nothing changed. If that deletion
  also fails, the message names the account so you can delete it under People.
- **The chat app did not answer.** The Console says the outcome is uncertain.
  Use **Refresh accounts** and check before trying again.
- **The email belonged to a deleted account.** Only an organization
  administrator can re-create it. The old grants are removed first, audited as
  `account_replaced`.
- **Account management isn't set up.** The Console needs the chat app's
  internal URL and `HUBZOID_GATEWAY_ADMIN_EMAIL`/`HUBZOID_GATEWAY_ADMIN_PASSWORD`.
  It refuses to write through a public URL.

Console account creation works on a deployment whose agents still use legacy
access. Access for those agents keeps coming from Open WebUI groups.

### Account actions for organization administrators (implemented)

A person's **Details** has a **Chat account** section with the live chat-app
role. Organization administrators can:

- **Approve** a pending signup.
- **Reset password.** The new password is shown once. Open WebUI signs the
  person out of other sessions.
- Switch the **chat-app administrator role** (`user` or `admin`). This does not
  grant Console or agent access.
- **Delete account.** Type the email to confirm. Every grant is removed first,
  then the account and its chats. The last organization administrator cannot
  be removed.

You cannot change your own account here, nor the Console's service account.
Changing an account's email is not offered: grants are keyed on the email, so
create a new account instead.

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
- change public access or an organization administrator's access
- remove all of someone's access in an agent when that person holds a
  capability the delegate does not
- reset passwords, approve, change roles, delete or block accounts

A delegate can create a normal account, but only with at least one grant in an
agent they manage. Organization administrators keep their full scope.

## Management API (implemented)

The Console's API under `/portal/api` is also the management API. Besides the
session cookie, every endpoint accepts `Authorization: Bearer sk-...` with the
caller's Open WebUI API key. The key is verified against Open WebUI's key table,
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

1. Open **Agents → the agent → Access → Add person**.
2. Enter the exact email the person uses for chat sign-in.
3. Choose capabilities, review the changes, and save.
4. Share the chat URL. No account or invitation is created by an access grant.
   If they have no sign-in yet, create one with **People → Add account**, or
   approve their pending signup from their Details.
5. Verify with that person's account that allowed agents appear and a disallowed
   tool stays unavailable.

Changes apply atomically. If someone else edited access first, reload and review
again. A lost response can leave the save uncertain; refresh before retrying.
Use **People** to distinguish an administrator block from a pending or unavailable
chat account. Reactivation does not recreate grants removed by an explicit block.

## Restrict a tool

Put the tool factory in `restricted/<capability>.py`. All tools returned by that
module share its capability name. Keep credentials in `restricted/.env` or your
runtime's secret injection. Do not return secrets from a tool or workflow step.

Optional `identity/permissions.yaml` gives capabilities useful labels,
descriptions and sensitivity indicators. Fields are optional; no new required
hub file is introduced. The Console discovers capability names without executing
the restricted Python modules.

A Python workflow acts as `workflow:<function>`. A Markdown task acts as
`workflow:md:<task>`. Grant the service identity only the tools the task needs.
In Console, open **Add person**, select **Service**, and enter that exact
identity (for example `workflow:md:daily-notes`). This grant does not create a
credential or start the task.

The built-in **Save shared knowledge** capability (`curator`) controls
`remember`. Grant it in Console to people or workflow identities that should
create or replace documents under `knowledge/_learned/`. These documents are
shared agent knowledge, not private conversation memory. No `restricted/curator.py`
file is required to make this capability appear.

## Existing hubs

Unmigrated hubs retain their legacy group/roster rules. The Console labels this
mode and does not pretend its draft grants have replaced the old authority.
Follow the backup, dry-run and activation procedure in [ADMINISTRATION.md](ADMINISTRATION.md).
The older mechanics remain documented in [legacy-access.md](legacy-access.md)
for migration and diagnosis only. Do not use that guide to configure a new
managed hub.

## Hiding the Open WebUI Users page (implemented, off by default)

Set `HUBZOID_HIDE_OWUI_USERS=true` in the environment of the process that runs
the edge (the gateway, or `hubzoid run`) once Console account management works
on that deployment. Then:

- Opening Open WebUI's Users page (`/admin/users`, `/admin/users/overview`) lands
  on Console **People**. Open WebUI's **Admin Panel** entry opens that page too.
  Its settings stay at `/admin/settings`.
- Browser writes to Open WebUI's account admin API get 403: `POST
  /api/v1/auths/add`, `POST /api/v1/users/{id}/update` and `DELETE
  /api/v1/users/{id}`. A person's own settings (`/api/v1/users/user/...`) are
  unaffected.
- The Groups tab (`/admin/users/groups`) stays, because legacy agents use groups.
- Hubzoid's own calls go to the internal URL and never pass the edge.

## Planned, not built yet

- Connections started from chat (`connector_<app>` capabilities and the
  `hz_connect` journey) are a separate work package. The edge hook they need is
  in place and inert without its cookie.
- `hubzoid doctor` will warn when Google sign-in is configured without
  `OAUTH_MERGE_ACCOUNTS_BY_EMAIL=true`.
- The Console does not yet list a person's connections.

## Administration boundary

Open WebUI's account, sign-in, connection and upstream administration controls
remain available. Its functions and automations are Open WebUI features; they do
not edit Hubzoid's hub-folder workflows or runtime configuration. Configure hub
workflows in `schedule/` or `workflows/`, and inspect them in the Console.

Agent prompts are not an authorization boundary. Do not expose the bridge port
directly, trust browser-supplied identity headers, or enable development identity
overrides on a shared deployment. See [DEPLOYING.md](DEPLOYING.md).
