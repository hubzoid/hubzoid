# Access management

Open WebUI owns accounts and sign-in. Hubzoid owns access to agents and restricted
tools. Administrators use **Admin Console → Agents → Access**, with the same verified
chat session. Permissions are enforced outside the model.

## Accounts and grants

Public sign-up is closed by default. An administrator creates login accounts in
Open WebUI, then grants agent access in the Admin Console. **Add person** grants
permissions to an email address; it does not create an account or send an
invitation. Access can be prepared before the matching account exists.

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
| Organization-wide `manage_access` | All registered agents and organization administration |
| `manage_access` on specific agents | Only those agents; cannot change administrator rights or public access |
| Chat or restricted-tool access only | No Console access |
| Open WebUI admin role only | No automatic Console access; the designated first owner is bootstrapped as described below |

The chat sidebar link follows these permissions. Opening `/portal/` directly does
not bypass them: its API verifies the signed-in account and administrator scope.

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
   Create or approve the account in the chat app if your sign-in policy requires it.
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

## Administration boundary

Open WebUI's account, sign-in, connection and upstream administration controls
remain available. Its functions and automations are Open WebUI features; they do
not edit Hubzoid's hub-folder workflows or runtime configuration. Configure hub
workflows in `schedule/` or `workflows/`, and inspect them in the Console.

Agent prompts are not an authorization boundary. Do not expose the bridge port
directly, trust browser-supplied identity headers, or enable development identity
overrides on a shared deployment. See [DEPLOYING.md](DEPLOYING.md).
