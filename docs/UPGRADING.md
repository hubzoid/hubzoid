# Upgrading

Hubzoid 2.0 has one chat interface, the Hubzoid web app. Open WebUI is no
longer an option: there is no `hubzoid[openwebui]` extra, no `HUBZOID_UI`
switch and no account sync. A deployment that still needs Open WebUI stays on
the 1.x line (`pip install "hubzoid<2"`). The sections for 1.x releases below
describe those releases as they were.

- [Upgrading to 2.0.2](#upgrading-to-202)
- [One run mode: every hub supervised, every setting in one file](#one-run-mode)
- [Upgrading to 2.0: Open WebUI removed](#upgrading-to-20-open-webui-removed)
- [Upgrading to 1.3.2](#upgrading-to-132)
- [Upgrading to 1.3: workflows from chat](#upgrading-to-13-workflows-from-chat)
- [Upgrading to 1.2: MCP connectors in the Console](#upgrading-to-12-mcp-connectors-in-the-console)
- [Upgrading to 1.1 from 1.0.x](#upgrading-to-11-from-10x)
- [Upgrading to 1.0.1 from 0.9.x](#upgrading-to-101-from-09x)

## Upgrading to 2.0.2

```bash
pip install -U "hubzoid==2.0.2"
hubzoid restart <hub> [<hub>…]     # or restart the service
```

No database or file changes. What behaves differently:

- **Changing agent files from the browser is Beta and off by default.**
  Proposing and approving hub changes, the agent's `remember` and
  `propose_hub_change` tools, and saving connectors, channels, settings,
  approval rules and budgets in Settings now answer that it is off. People can
  still read everything there; the CLI and your editor work as before. To keep
  the 2.0.1 behaviour, set `in_app_editing: true` in `hubzoid.yaml` (or
  `HUBZOID_IN_APP_EDITING=true`). See [DEPLOYING](DEPLOYING.md#in-app-editing).
- **Settings is reorganised** into Personal and Manage. Old addresses and
  bookmarks open the new place: Health is under Organization, Runs and the audit
  log under Activity, My work items in the Inbox.
- **Work items on a hub whose folder name has capitals** (`Customer-Desk`) were
  queued where no worker listened in 2.0.1. After the upgrade they run, including
  ones already waiting.
- **Codex:** `codex-local` works with Codex CLI 0.163.0 as well as 0.147.0. Other
  versions are refused with the versions to install. `HUBZOID_CODEX_BIN` names a
  binary that is not first on `PATH`.
- **Schedules off on a machine** (`HUBZOID_SCHEDULES`): the start now warns when
  an agent has scheduled workflows, the missed-run catch-up no longer runs, and
  Settings says the schedules are off instead of showing a next run.
- **API reference** (`/docs`, `/openapi.json`) is titled Hubzoid API with the
  real version and lists only the public API. See [the API guide](api.md).
- **Embedded chat** (AG-UI) is new. See [embedding](embedding.md).
- `typer` from 0.26.8, so `hubzoid[parsing]` (docling) installs alongside.

## One run mode

`hubzoid run` now runs one hub or many: each hub in its own process, restarted
on its own, behind one public port, with `hubzoid status`, `restart`, `reload`,
`stop`, `start` and `logs` for a running deployment. And every setting has one
home:

| Setting | Its home now | Before |
|---|---|---|
| Hub list, public URL, sign-in, owner | `hubzoid.yaml` in the deployment folder | `hubzoid gateway` arguments and environment |
| Brand | `brand:` in `hubzoid.yaml`, images in `branding/` | drafts, reviews and activations in the database |
| Chat defaults | `chat:` in `hubzoid.yaml` | `chat/config.yaml` applied into the database |
| Diagnostics limits | `diagnostics:` in `hubzoid.yaml` | `admin.yaml` planned and applied |
| Connectors | `connectors/<id>.yaml` in the deployment folder, secrets in `.env`; editable in Settings or by hand | added in Settings, stored in the database |
| Channels (Slack app, WhatsApp, Telegram, webhook) | a file in the hub's `channels/` | `--slack`, `--whatsapp`, `--telegram`, `--webhook` |
| Keys a person types | `.env` (or the host's environment) | also storable in Settings |
| Instructions and their history | the files, changed through hub changes; Git history | Workspace drafts and versions in the database |
| Phone numbers, who may ask from a channel | people and grants in Settings | also `identity/access.csv` |

`chat/config.yaml`, `branding.yaml` and `admin.yaml` are still read for this
release when `hubzoid.yaml` has no such section, and a hub whose `.env` alone
turns on a 1.x channel still gets it (with a notice). `hubzoid gateway` works
for one release and prints the `hubzoid.yaml` it amounts to.

### Steps (offline, once)

1. **Let waiting work finish.** Workflow runs started by the old version are
   cancelled when a hub starts on new code, so finish or cancel them first
   (`hubzoid schedule status`, the Workflows page).
2. **Stop everything**: the gateway unit, every per-hub unit and every inbound
   unit (`systemctl stop`, then `systemctl disable` them).
3. **Back up** each hub and the state folder: `hubzoid backup`, then
   `hubzoid backup verify` the archive. Keep the deployment key with it.
4. **Install** the new version, then run, in the deployment folder:
   ```bash
   hubzoid upgrade --dry-run --data-dir ./gateway-data    # what it would write
   hubzoid upgrade --data-dir ./gateway-data
   ```
   It writes `hubzoid.yaml` (from the gateway's manifest), the brand, chat
   defaults and diagnostics limits kept in the database, Settings connectors
   (`connectors/<id>.yaml`, secrets into `.env`), keys typed into Settings
   (into their hub's `.env`), a channel file for each 1.x channel a `.env`
   turns on, and exports Workspace drafts and published versions to
   `<state>/exports/`. It never overwrites a file and never prints a secret.
   Then it brings the database to the new schema, which drops the database
   copies; that step refuses to run while anything kept only there has not
   been written out.
5. **Review and commit** the new files (`git diff`). Remove from each hub's
   `.env` what is now set once: `HUBZOID_AUTH` (now `sign_in:`), `WEBUI_NAME`
   (now `brand.identity.name`), and the retired 1.x names doctor lists. Move
   `ENABLE_SIGNUP` and sign-in client settings to the deployment's `.env`.
   `BRIDGE_PORT` and `BRIDGE_API_KEYS` can go.
6. **One unit**: `hubzoid service install --data-dir ./gateway-data ...`
   ([deploying](DEPLOYING.md#5-systemd-unit-one-unit-for-every-hub)), then
   `systemctl enable --now hubzoid` and `hubzoid status`.

**Going back:** restore the backup (databases, state folder and deployment key
together), reinstall the previous version and re-enable the old units.

## Upgrading to 2.0: Open WebUI removed

### What changes
- **One interface.** `hubzoid run` and `hubzoid gateway` start the Hubzoid web
  app, or no UI with `--no-ui`. Open WebUI is never started or installed.
- **The Console is in Settings.** The management screens (Agents, People &
  access, Activity, Schedules, Workspace settings, Health, Advanced settings)
  are sections of the web app's Settings page. The old address
  `/portal/` sends people to the same section there, so bookmarks keep working.
- **Accounts are the web app's own.** Sign-in, Google sign-in and sign-up use
  the web app's settings ([accounts and sign-in](auth.md)). Open WebUI API keys
  no longer work: people make personal API keys (`hzp_…`) in Settings →
  Security. MCP clients connect through OAuth ([MCP server](mcp-server.md)).
- **Access comes from grants only.** Open WebUI groups and
  `BYPASS_MODEL_ACCESS_CONTROL` give no access. Someone without a grant signs in
  to "No agents yet". The migration's dry run counts these people; grant them
  access in Settings → People & access before they move.
- **Administrators set up two-step sign-in** at their first 2.0 sign-in (an
  authenticator app), unless Settings → Security says otherwise.
- **Behind a CDN, list it.** The public port believes `X-Forwarded-For` only
  from loopback and, while `HUBZOID_TRUSTED_PROXIES` is unset, private
  networks; setting it lists exactly which proxies to believe. With
  Cloudflare (or another CDN) in front, set `HUBZOID_TRUSTED_PROXIES` to its
  address ranges, or to `*` if the port is reachable only through it.
  Otherwise everyone counts as one of the CDN's few addresses, and ten failed
  sign-ins there lock sign-in for everyone behind it for 15 minutes.
- **Open WebUI settings are named, never ignored.** At start, and in
  `hubzoid doctor`, Hubzoid lists every Open WebUI setting it finds in the
  environment or a hub's `.env`.

### Settings that stop the start until changed

These decide who can sign in or who owns the deployment. Dropping them
silently could turn sign-in off or lose the owner, so the start stops with a
message naming each one, even when its value is blank or `false`:

| Setting | Change it to |
|---|---|
| `HUBZOID_UI` set to `openwebui`, `open-webui`, `owui` or `legacy` | Remove it. To keep Open WebUI, stay on 1.x. |
| A deployment recorded in Open WebUI mode (`ui_mode` in the gateway's deployment record) | Move it with the steps below, or stay on 1.x. |
| `WEBUI_AUTH` | `HUBZOID_AUTH`, same value |
| `WEBUI_URL` | `HUBZOID_PUBLIC_URL`, same value |
| `WEBUI_ADMIN_EMAIL`, `HUBZOID_GATEWAY_ADMIN_EMAIL` | `HUBZOID_ADMIN_EMAIL`, same value |
| `WEBUI_ADMIN_PASSWORD`, `HUBZOID_GATEWAY_ADMIN_PASSWORD` | `HUBZOID_ADMIN_PASSWORD`, or remove it once the owner has signed in |

### Settings that only warn
- `WEBUI_NAME` is still read as the deployment's name until one is set in
  Settings → Brand. In 1.x it also named each hub's agent: put that name in the
  hub's `AGENTS.md` frontmatter as `display_name:` (for example
  `display_name: Reports Hub`). Then remove it.
- `LICENSE_KEY` (Open WebUI's licence key) is not used. Remove it.
- `HUBZOID_OWUI_DB` is read only by `hubzoid migrate openwebui`. Remove it once
  the move is done.
- `DEFAULT_USER_ROLE`: new accounts follow `ENABLE_SIGNUP` and
  `ENABLE_OAUTH_SIGNUP`. Remove it.
- Open WebUI's own settings (`WEBUI_SECRET_KEY`, `ENABLE_*` feature switches,
  `USER_PERMISSIONS_*`, `OPENAI_API_*`, `DEFAULT_MODELS`, `RAG_*`,
  `AUDIO_STT_ENGINE` and the Hubzoid settings that configured the Open WebUI
  integration, such as `HUBZOID_OWUI_PORT` and `BYPASS_MODEL_ACCESS_CONTROL`)
  have no effect. Chat defaults live in Settings → Workspace settings and
  `chat/config.yaml`. Remove them.

These names keep their meaning in the web app: `ENABLE_SIGNUP`,
`ENABLE_LOGIN_FORM`, `ENABLE_OAUTH_SIGNUP`, `GOOGLE_CLIENT_ID`,
`GOOGLE_CLIENT_SECRET` and the `OAUTH_*` settings.

### Steps for a deployment on Open WebUI
1. **Back up** on 1.x: `hubzoid backup ./my-hub --out pre-2.0.tar.gz`, plus
   `pg_dump` for PostgreSQL ([backup](BACKUP.md)). Keep a protected copy of
   each `.env`.
2. **Install** 2.0: `pip install -U "hubzoid>=2"`.
3. **Check readiness** (reads only): `hubzoid migrate readiness ./my-hub`.
   For a gateway, pass its data folder (for example `./gateway-data`).
4. **Rehearse on a copy**: `hubzoid migrate openwebui ./my-hub --rehearse
   ../rehearsal --apply`. For a gateway, pass its data folder: every hub
   folder, the gateway's Open WebUI database, uploads and operational store are
   copied (SQLite), with a manifest naming the copies, and the copy is
   migrated. Start the copy on spare ports and sign in as a few real people.
5. **Dry run, then apply** the move of accounts, conversations and share links,
   with the deployment stopped: `hubzoid migrate openwebui ./my-hub`, then
   `hubzoid migrate openwebui ./my-hub --apply`. The command reads Open WebUI's
   database and never changes it. Read the warnings: people without agent
   access, and a configured owner (`HUBZOID_ADMIN_EMAIL`) that has no account
   (`--apply` creates it when `HUBZOID_ADMIN_PASSWORD` is set). Open WebUI's
   account for running without sign-in (`admin@localhost`) is imported blocked
   and not as an administrator. [What moves and what does not](#what-moves-and-what-does-not)
   still applies.
6. **Rename** the settings in the first table, and remove the ones that warn.
7. **Start** with `hubzoid run my-hub` (or `hubzoid run` with a `hubzoid.yaml`), then run
   `hubzoid doctor my-hub` and sign in as the owner and as an ordinary user.

What 2.0 does not yet cover compared with Open WebUI is listed in the
[2.0 release notes](release-notes/2.0.md).

### Going back
Reinstall 1.x (`pip install "hubzoid<2"`), restore the backup taken in step 1
and restore the old `.env` files. Open WebUI's database was never changed.

## Upgrading to 1.3.2

### Memory settings for deployments staying on 1.x

With Open WebUI 0.11.4, `ENABLE_MEMORY` is not the memory switch. A deployment
staying on 1.3.2 can disable Open WebUI memory explicitly in its operator
configuration with `ENABLE_MEMORIES=false`, `ENABLE_MEMORY_SYSTEM_CONTEXT=false`
and `ENABLE_MEMORY_BACKGROUND_REVIEW=false`, then restart. Existing saved
memories remain in the Open WebUI database. The web-search switch is
`ENABLE_WEB_SEARCH`, not `ENABLE_RAG_WEB_SEARCH`.

These are historical 1.x instructions. Hubzoid 2.0 removes the Open WebUI
runtime; it does not apply these defaults or restore that interface.


Back up the Hub and operational state, install `pip install -U 'hubzoid==1.3.2'`
(retain `[openwebui]` if used), then restart the gateway and bridges. There are
no new required Hub files or database schema changes. Claude failover is opt-in;
one-credential deployments keep their existing behavior. See
[Claude credential failover](DEPLOYING.md#claude-credential-failover) before adding
backups. Cooldowns are operational metadata and survive restarts.

Windows locking and process cleanup are corrected in this release. Use a native
Windows Python 3.11/3.12 environment, or a separate Linux environment inside WSL;
do not share a virtual environment between them. Do not install `fcntl` on Windows.

## Upgrading to 1.3: workflows from chat

These changes first shipped in **1.3.1**; 1.3.0 was skipped. Existing Hub folders need no new
required fields. Back up before upgrading (`hubzoid backup <hub> -o state.tar.gz`).
In the environment that runs your deployment, install `pip install -U 'hubzoid==1.3.1'`
(retain `[openwebui]` if you use that extra) and restart the gateway and every bridge so the bundled
Console and runtime agree. If you tried pre-release main, ask again for any
pending confirmations. New chat proposals bind to the account and chat that
requested them; confirm in that same conversation within ten minutes.

**GitZoid is included as a new template.** Existing Hubs do not receive template
updates automatically. If you tried an earlier GitZoid checkout, back up its
Hub and state, compare the new template, and preserve your settings, approved
rules and secrets. Start in preview mode and run `setup_check` before enabling
GitHub publishing. See the [sample guide](../hubzoid/templates/gitzoid/README.md).

No Hub-file migration is required. The report features are off until a workflow opts in
(`inputs=`, `as_tool=`, `schedulable=`, and optionally `key=` or `audience="domain"`) and an
admin grants the new capabilities. These reach every hub:

- **New tables** for report series, personal schedules and proposals. They are
  created on first start, like earlier migrations. Back up first as usual:
  `hubzoid backup <hub> -o state.tar.gz`.
- **Latest links.** `/portal/latest/<agent>/<key>` now serves the page and keeps
  it current, instead of redirecting to one copy. Bookmarks keep working.
- **Daylight saving.** A workflow scheduled at a fixed time in a zone with
  daylight saving now runs at that wall-clock time on the changeover day.
- **One portal for everyone.** `/portal/` now opens for every signed-in person,
  showing their own schedules, connections and agents. Only admins get the
  admin pages, as before. In Open WebUI mode the sidebar's Hubzoid link shows for
  everyone signed in: "Admin Console" for admins, "My workspace" for
  the rest. `/portal/connections` now opens the portal's Connections tab.
- **MCP timeouts.** On the OpenAI Agents SDK and Codex runtimes an MCP tool call
  now gets 30 seconds instead of 5. Set `HUBZOID_MCP_TIMEOUT` to change it; a
  server's own `client_session_timeout_seconds` still wins. The Claude runtime is
  unchanged unless you set it.

Company links need company sign-in and three settings; see
[Company links](reports-and-email.md#company-links).

After restarting, run a permitted report, open its result, and check a personal
schedule in **My workspace → Schedules & reports**. Changing the audience or
named readers creates a new copy even when its contents are unchanged; old
copies retain their access. Revoke or delete old copies separately if needed.
To roll back, restore the backup and reinstall your previous package version.

## Upgrading to 1.2.2: a gateway at more than one address

Nothing to change, unless people reach your gateway at more than one address.
Then restart the gateway once, and then the first hub's agent, which serves
every agent's connection pages. Every agent then accepts each address the
deployment names: the gateway's `--public-url` and `HUBZOID_ALLOWED_ORIGINS`,
and each hub's own `HUBZOID_PUBLIC_URL`. A connector sign-in returns to the
address it started on. A `HUBZOID_ALLOWED_ORIGINS` you added to a hub's `.env`
for this can stay or go.

## Upgrading to 1.2.1: WhatsApp and Slack services in a gateway

Nothing to change, unless a WhatsApp, Telegram or Slack service behind a
gateway runs from a unit printed by `hubzoid inbound systemd` or
`hubzoid slack systemd`. Those units could write only the hub folder, not the
gateway's data folder where the shared database lives. Add that folder to the
unit and restart only that service (see
[Running](inbound-surfaces.md#running)):

```
mkdir -p /etc/systemd/system/<unit>.service.d
printf '%s\n' '[Service]' 'ReadWritePaths=<gateway data folder>' > /etc/systemd/system/<unit>.service.d/shared-store.conf
systemctl daemon-reload && systemctl restart <unit>
```

Until then the WhatsApp service logs one error at start and runs without
connection confirmations. If you set `HUBZOID_CONNECT_JOURNEY=false` to quiet
1.2.0's repeated errors, remove it once the folder is added.

Phone numbers recorded on people in the Console now identify WhatsApp senders
in a gateway too. Before, only the hub's own `identity/` roster did. Check that
those numbers belong to the right people.

## Upgrading to 1.2: MCP connectors in the Console

Every MCP connector is added in the Console (Agents, the agent, Connectors) in
both UI modes, and Hubzoid holds each person's connection. Open WebUI's own MCP
servers (External Tool Servers with `OWUI_NATIVE_MCP=true`) are no longer read.

**Web app mode:** nothing to do.

**Open WebUI mode**, if you registered MCP servers in Open WebUI:

1. Back up: `hubzoid backup <hub> -o state.tar.gz` (it includes Open WebUI's
   data). Keep the deployment key (`HUBZOID_SECRET_KEY` or the key file).
2. Note each server's URL and ID, then remove it in Open WebUI: Admin Panel,
   Settings, Integrations, External Tool Servers. Do this before upgrading:
   afterwards the edge refuses changes there.
3. On a gateway, make sure `ENABLE_FORWARD_USER_INFO_HEADERS` is not `false`.
   Connectors need each person's Open WebUI account id from it.
4. Upgrade and restart.
5. In the Console, add each server again with the same ID (Add connector),
   offer it in the agents that need it, and grant **Connect <name>**.
6. People connect once, from the link the agent sends (`connect_account` is
   on by default) or from `/portal/connections`.

`OWUI_NATIVE_MCP` is ignored and logged. If it was on, settings changed in Open
WebUI's admin screen while it was on may stop applying, because
`ENABLE_PERSISTENT_CONFIG` is off again. Put any you need in `.env`. People's
old Open WebUI connections stay in Open WebUI's database until removed there.
Hubzoid does not read them. Revoke them at the provider if that matters.

**Connector tool names on OpenAI Agents and Codex** change from the server's
own name (`search`) to `mcp__my_<id>__<tool>` (`mcp__my_gmail__search`), as
Claude already named them. Update any agent instructions or skills that name a
connector tool directly. Hub tools and hub-folder servers keep their names.

To go back, restore the backup and reinstall the previous version. This
release never writes to Open WebUI's database.

## Upgrading to 1.1 from 1.0.x

Hubzoid 1.1 adds the Hubzoid web app as the default chat UI: Hubzoid's own
accounts and sign-in, chat, conversation history and personal MCP connections,
for one hub or a gateway. Open WebUI stays a supported chat UI (Open WebUI
mode). In both, every agent's access is managed in the Console. Hub files
(`AGENTS.md`, skills, knowledge, `schedule/`, `workflows/`) need no changes. The [release notes](release-notes/1.1.0.md)
summarize the release and its known limits.

Choose one path before you start:

- **Move to the web app** (the default). Run `hubzoid migrate openwebui` to
  move accounts, conversations and share links. Access is already in the
  Console and stays as it is. Steps below.
- **Keep Open WebUI.** Install the extra and set Open WebUI mode, and chat works
  as in 1.0.x:

  ```bash
  pip install -U "hubzoid[openwebui]"
  # in each hub's .env, or the gateway's environment:
  HUBZOID_UI=openwebui
  ```

  With Docker, build with `--build-arg WITH_OPENWEBUI=true`.

  For an existing Console-managed deployment, this is the least disruptive
  first upgrade: preserve Open WebUI history and personal connections, and
  keep the existing Console grants. Set the mode in the gateway environment
  **and every bridge**. No access migration is needed.

Restart the gateway and all bridges on the same Hubzoid version. With
`--no-bridges`, start the upgraded bridges before the gateway. Its readiness
check verifies both the hub name and model id; an older or different service
on the expected port is refused.

### What changes

| Change | What you do |
|---|---|
| `pip install hubzoid` no longer installs Open WebUI or PyTorch. Upgrading an existing environment leaves the installed Open WebUI in place, but it is not used unless `HUBZOID_UI=openwebui`. | Nothing, unless you keep Open WebUI (above). |
| `hubzoid run` and `hubzoid gateway` serve the Hubzoid web app by default. | Follow the steps below. |
| With sign-in on (`HUBZOID_AUTH=true`, or the 1.0 name `WEBUI_AUTH=true`), a hub or gateway whose Open WebUI database has accounts and whose Hubzoid store has none stops at start and prints the two ways forward. In local mode `hubzoid run` only prints that the old chats can be imported. | Migrate, or keep Open WebUI. |
| Hubzoid owns sign-in. Existing passwords keep working. Google, Microsoft and OpenID Connect use the same variable names and callback paths as Open WebUI, so existing provider registrations keep working. `WEBUI_SECRET_KEY` is not used in the default mode. | Everyone signs in once after the move: sessions do not move. See [authentication](auth.md). |
| Hosted MCP accepts only OAuth credentials issued by Hubzoid after sign-in and consent. Open WebUI API keys no longer authenticate `/mcp`. `MCP_PUBLIC_URL` is required with `MCP_SERVER=true`. | Reconnect each MCP client ([below](#mcp-clients-move-from-api-keys-to-oauth)). |
| In the web app, personal MCP connections are run by Hubzoid. Connections made in Open WebUI are not moved. | An organization administrator adds each server in **Console → Agents → the agent → Connectors**, then each person connects again on **Account → Connections**. See [MCP connectors](mcp.md#connectors-in-the-console-both-ui-modes). Since 1.2 Open WebUI mode uses the same Console connectors. |
| Every agent's access is managed in the Console, in both chat UIs. Open WebUI groups, roster groups and `MCP_ACCESS_GROUP` grant nothing. A call to the bridge without a verified person is refused. | Run `hubzoid doctor`: it warns about an agent nobody may use. Grant **Use this agent** to the people who need it. |
| New secret: the deployment key (`HUBZOID_SECRET_KEY`, or the `secret.key` file created on first use) encrypts personal connection tokens and signs identity between Hubzoid processes. Backups leave it out unless asked. | Back it up separately ([backup](BACKUP.md)). |
| A local `hubzoid run` refuses a network `--host` without sign-in. | Turn sign-in on, or set `HUBZOID_ALLOW_UNAUTHENTICATED_NETWORK=true` on a network you trust. |
| Download links in the web app expire after 7 days by default (`HUBZOID_ARTIFACT_LINK_TTL`, `0` for never). The owner of a conversation can always download its files while signed in. | Nothing. Open WebUI mode keeps links that never expire. |
| `hub.call_agent` returns only the agent's answer. Tool lines, reasoning, download footers and error markers no longer appear in workflow data. | Check workflows that parsed those lines. |
| Markdown task and scheduled eval run ids name the hub (`md:<task>:<slot>@<hub>`). Runs queued under the old ids are still listed, re-queued and cancelled. | Nothing. |
| The Docker image is slim (no ffmpeg, PyAV build tools or PyTorch). The compose file turns sign-in on. | For Open WebUI mode, build with `--build-arg WITH_OPENWEBUI=true` (`HUBZOID_WITH_OPENWEBUI=true` with compose). See [deployment](DEPLOYING.md). |

### Move a hub to the web app

1. **Install** the new version in the same environment:
   ```bash
   pip install -U hubzoid
   ```
2. **Dry run** the migration. It only reads Open WebUI and changes nothing:
   ```bash
   hubzoid migrate openwebui ./my-hub
   ```
   The report counts people, external sign-in links, conversations,
   messages, attachments and share links, and lists anything blocking. It never prints message content or emails.
   `--verbose` adds the ids of skipped items, `--json` prints the report as
   JSON. The command exits 2 when something blocks the migration. Useful
   options:
   - `--model-alias OLD=AGENT` imports conversations of an old model id into
     an agent of this deployment.
   - `--owui-db URL` reads another Open WebUI database (SQLAlchemy URL).
   - `--rehearse <empty folder>` copies a standalone hub and migrates the copy
     (SQLite only). The original is only read.
3. **Back up**, with the deployment still on 1.0.x data:
   ```bash
   hubzoid backup ./my-hub --out pre-1.1.tar.gz
   ```
   For PostgreSQL, also `pg_dump` ([backup](BACKUP.md)). Keep a protected
   copy of each `.env`.
4. **Stop** the hub (or the gateway and every bridge). A running bridge waits
   on the database while the migration writes.
5. **Apply**:
   ```bash
   hubzoid migrate openwebui ./my-hub --apply
   ```
   It writes accounts, conversations and shares in one transaction, so a
   failure changes nothing. Running it again is safe:
   rows are matched by id, changes made in Hubzoid since are kept, and what
   was deleted in Hubzoid is not brought back.
6. **Start** in the default mode: `hubzoid run my-hub`. Remove `HUBZOID_UI`
   from the hub's `.env` if it was set.
7. **Check**: sign in as an ordinary user, open a moved conversation, use an
   agent with a restricted tool, open the Console (People, Agents),
   and run `hubzoid doctor my-hub`.

### What moves and what does not

Moved:

- **People**, with their id, email, name, role and approval state. Password
  hashes move as they are, so nobody resets a password. Google, Microsoft and
  OpenID Connect links move. Accounts deactivated in Open WebUI are imported
  but blocked. A later run also blocks accounts deactivated there since,
  unless an administrator reactivated them in the Console.
- **Conversations**, with their ids, owners, branches, current branch, titles,
  archive state and attachments. Tool lines, tool errors and reasoning written
  by 1.0.x become proper message parts. Files only in Open WebUI's storage are
  copied into the hub's chat folders.
- **Share links**, with their ids, so `/s/<id>` keeps working.

Not moved: access (it is already in the Console), Open WebUI groups (they
grant nothing), sessions (everyone signs in once), Open WebUI API keys, personal
MCP connections and their tokens, folders (flattened), tags, pins, ratings and
feedback, notes, channels, Open WebUI knowledge and memories, and per-chat
parameters. GitHub and Feishu sign-in links are not imported, because the web
app does not offer those providers.

Also note:

- **Share links** that Open WebUI kept private or restricted stay disabled.
  Their snapshots and original audience are retained. The owner can open the
  conversation and explicitly share it again. New links open for any signed-in
  person of the deployment, as the Share dialog explains. Previously public
  Open WebUI links keep working for signed-in people.
- **Tool arguments** in moved conversations are the short previews 1.0.x
  stored. Full arguments were never kept.
- **`admin@localhost`** from Open WebUI's sign-in-off mode is imported
  without its well-known password. It stays the local owner.
- **An email clash** (a Hubzoid account with the same email but another id)
  blocks the migration. Nothing is merged.
- **Microsoft links** without a tenant GUID in `MICROSOFT_CLIENT_TENANT_ID`,
  and OpenID Connect links without a discovery URL in `OPENID_PROVIDER_URL`,
  carry a placeholder issuer until the person signs in again.
- **Open WebUI API keys stop working.** In the Hubzoid web app mode the
  Console's management API ignores `Authorization: Bearer sk-...`, even while
  the old Open WebUI database is still on disk, so a key-only request gets 401.
  Scripts sign in with `POST /api/auth/login`, keep the `hz_session` cookie,
  and send an `Origin` header with every write. Keys keep working only in
  Open WebUI mode (`HUBZOID_UI=openwebui`).

### The first sign-in after the move

Everyone signs in again at `/auth`, with the same password or the same Google,
Microsoft or OpenID Connect account. A person whose account was waiting for
approval in Open WebUI still waits. Administrators keep the Administrator
role. If the deployment used sign-in off, nothing changes: you are the local
owner.

### MCP clients move from API keys to OAuth

1. Remove the old configuration that sent an API key, for example
   `claude mcp remove hubzoid --scope user`. This does not delete the key.
2. Make sure the hub's `.env` has `MCP_SERVER=true` and `MCP_PUBLIC_URL` set
   to the exact public address of the endpoint: `https://hub.example.com/mcp`,
   or `https://hub.example.com/b/<slug>/mcp` for a gateway hub. A local
   `hubzoid run` sets both for you.
3. Add the server without a header and authenticate:
   ```bash
   claude mcp add --transport http --scope user hubzoid https://hub.example.com/mcp
   ```
   In Claude Code, run `/mcp`, sign in to Hubzoid in the browser if asked, and
   choose **Allow connection**.

Clients that can only send a fixed `Authorization` header cannot connect any
more. See [hosted MCP](mcp-server.md).

### A gateway

Do the same steps with the gateway: dry run, back up, stop the gateway and
every bridge, apply, start. Pass any hub of the gateway or its data folder
(the one holding `deployment.json`):

```bash
hubzoid migrate openwebui ./gateway-data            # dry run
hubzoid backup ./sales --out pre-1.1.tar.gz        # a hub in a gateway backs up the whole gateway
hubzoid migrate openwebui ./gateway-data --apply
hubzoid gateway ./sales ./support --data-dir ./gateway-data
```

Set `HUBZOID_AUTH` once in the gateway's environment. A hub `.env` that sets a
different `HUBZOID_UI`, `HUBZOID_AUTH` or `HUBZOID_SECRET_KEY` stops the
gateway, and so do hubs that disagree on `WEBUI_AUTH` when `HUBZOID_AUTH` is
not set.

Rehearse on a copy first. `--rehearse` does not handle a gateway, so make the
copy from a backup:

1. `hubzoid backup ./sales --out state.tar.gz`
2. `hubzoid restore state.tar.gz --move /srv/hubs=/srv/rehearsal` (the old
   folder, then a scratch folder).
3. Copy each hub's content into the restored hub folders: `AGENTS.md`,
   `restricted/` and `identity/` (backups leave hub content to git, and the
   access plan reads them).
4. Run `hubzoid migrate openwebui` on the restored copy, first as a dry run,
   then with `--apply`, and check the result before migrating the real
   gateway.

### Going back

Stop everything and restore the pre-upgrade backup, then install the previous
version:

```bash
hubzoid restore pre-1.1.tar.gz
pip install "hubzoid==<previous version>"
```

The migration never changes Open WebUI's data, so Open WebUI mode
(`hubzoid[openwebui]` with `HUBZOID_UI=openwebui`) is also a way back on 1.1.
Changes made in the web app after the move stay in Hubzoid's store and do not
appear in Open WebUI.

## Upgrading to 1.0.1 from 0.9.x

> **From 1.0.x to the next release.** Nothing changes until someone grants the
> new capabilities. New agent tools for workflows (`workflows_view`,
> `workflows_manage`) and access (`access_tools`) appear in the Console under
> **Hubzoid tools**, unticked for everyone. `HUBZOID_MANAGEMENT_TOOLS=true`
> keeps its 1.0.x meaning for this release, with a deprecation warning: grant
> `access_tools` to the managers who use the access tools, then remove the
> setting. MCP clients now list only the gated tools the connected person may
> use. Markdown task and scheduled eval runs get hub-specific ids
> (`md:<task>:<slot>@<hub>`), so hubs sharing one PostgreSQL database no longer
> skip each other's work. Runs queued before the upgrade keep their old ids.

This release changes security defaults, moves markdown schedules onto the
workflow engine, runs scheduled work as ordinary accounts, adds the Admin
Console, versions Hubzoid's own database tables and changes the Docker image.
Hub files (`AGENTS.md`, skills, `schedule/*.md`, `workflows/`) need no changes.
Read the list below once, then follow the steps. The
[release notes](release-notes/1.0.1.md) summarize the release and its known
limits.

**Before you start:** on Python 3.12, workflows (markdown schedules and code
workflows) need SQLite 3.42 or newer, or PostgreSQL. Step 5 shows how to check.

### What changes, and what to do

| Change | What you do |
|---|---|
| Security: the edge no longer shares one visitor's sign-in cookie with another. | After upgrading, rotate `WEBUI_SECRET_KEY` (everyone signs in again) so any session that may have leaked ends. Personal Open WebUI MCP connections must then be reconnected. |
| The public bridge key `dev` is refused for downloads, and `hubzoid doctor` fails when `BRIDGE_API_KEYS` is unset. | Before restarting, set `BRIDGE_API_KEYS` to a long random value in each hub's `.env` (`openssl rand -hex 32`). The gateway hands each hub's key to the chat app when it starts. Hubzoid refreshes its owned connection URLs and keys at startup, including when native MCP enables persistence. Other saved integrations and settings are preserved. |
| Public sign-up is off by default. Hubzoid now sets `ENABLE_SIGNUP` and `ENABLE_OAUTH_SIGNUP` to `false` unless you set them. In 0.9.x the chat app let anyone who reached the sign-in page create an email account unless `ENABLE_SIGNUP=false` was set. | Existing accounts keep working. Add new people with **Add user** in the Admin Console, from an agent's Access page or from People. It creates the sign-in account and grants its access in one flow. Existing users get access with **Edit access**. To allow self sign-up again, set `ENABLE_SIGNUP=true` (email) or `ENABLE_OAUTH_SIGNUP=true` (SSO) explicitly. If the chat app keeps its settings in its database (for example with `OWUI_NATIVE_MCP=true`), check **Enable New Sign Ups** in its admin settings too, because a value saved there earlier can win. See [auth.md](auth.md). |
| Download links for files the agent made are signed. Links from earlier versions stop working. | Nothing. The files are still there; ask the agent for the link again. Optional expiry: `HUBZOID_ARTIFACT_LINK_TTL`. |
| Administrators can no longer open or export other people's chats in the chat app. | To allow it again, set `ENABLE_ADMIN_CHAT_ACCESS=true` and `ENABLE_ADMIN_EXPORT=true`. |
| The chat app keeps its own branding unless the hub has files in `branding/`. | Nothing, unless you relied on Hubzoid's logo. See [branding.md](branding.md). |
| OpenAI Agents SDK traces are no longer sent to OpenAI. | To keep them, set `HUBZOID_OPENAI_TRACING=true`. |
| Markdown tasks (`schedule/*.md`) run on the hub's workflow engine. Each hub gets `.hubzoid/dbos.db` (with PostgreSQL in `DATABASE_URL`, the engine uses that database instead), and runs appear in the Console. | Nothing. Timing, catch-up and commit behaviour are unchanged. Let running tasks finish before you stop the hub. |
| Hubzoid's tables are versioned and upgraded at the first start. A database written by a newer Hubzoid is refused. | Take the backup in step 4: going back to 0.9.x means restoring it. |
| Tool decisions are stored in the database instead of `logs/access-*.jsonl`. | Nothing. The old files are imported once and left in place. |
| Upgrading does not change who can use each agent. Existing hubs keep their Open WebUI group-based access until an operator migrates them to Console-managed access. | Nothing during the upgrade. The 1.0 cutover command (`hubzoid access migrate`) is gone in 1.1, where every agent is managed in the Console. |
| Gateway: a hub's `.env` no longer carries into the shared chat app. Sign-in and chat-app settings (`WEBUI_*` such as `WEBUI_SECRET_KEY` and `WEBUI_URL`, `DEFAULT_USER_ROLE`, `ENABLE_SIGNUP`, OAuth settings, `HUBZOID_PUBLIC_URL`) are still read from the hub `.env` files when the gateway's own environment does not set them. The gateway lists them when it starts. Model keys and restricted-tool secrets no longer reach the chat app, and `WEBUI_NAME` in a hub `.env` no longer overrides `--name`. | Nothing is required. When convenient, move those settings into the gateway's environment (with systemd, its drop-in) so the gateway stops listing them. Keep `WEBUI_SECRET_KEY` unchanged, or everyone is signed out. |
| Gateway: all bridges share one access and usage database, `hubzoid-operational.db` in the gateway's data directory. The gateway points each hub at it when it starts. | If the bridges run as their own services (`--no-bridges`), restart them once after the gateway's first start so they use the shared database. |
| The `claude` CLI and the MCP servers it starts no longer inherit restricted-tool values (`restricted/.env`), Hubzoid service secrets (bridge keys, `WEBUI_SECRET_KEY`, database URLs, SMTP credentials) or AWS credentials. Restricted tools run in the bridge and keep working. | Nothing, unless an MCP server in `.mcp.json` read one of those values from its environment. Give it the value in its own `env` block. |
| Open WebUI no longer receives `HUBZOID_*` settings. Under `hubzoid run`, values from `restricted/.env` no longer reach it either (sign-in settings found there still pass, with a warning). | Keep Open WebUI and sign-in settings in the hub `.env` or the gateway environment. |
| New features are off until enabled: agent-proposed access changes (`HUBZOID_MANAGEMENT_TOOLS`), hiding the Open WebUI Users page (`HUBZOID_HIDE_OWUI_USERS`), connection journeys (`HUBZOID_CONNECT_JOURNEY`) and AWS Secrets Manager layers. Console account management (**Add user**) works once the deployment's service account is configured. Nothing about existing accounts changes until someone uses it. An existing deployment keeps Open WebUI's Users page: only a gateway set up fresh with Console accounts records `hide_owui_users: true` in `deployment.json`. | Nothing. See [ADMINISTRATION.md](ADMINISTRATION.md#accounts-in-the-console), [mcp.md](mcp.md) and [DEPLOYING.md](DEPLOYING.md) to turn them on. |
| Console-managed access goes to named people. New "Everyone signed in" grants are refused everywhere, including `hubzoid grant '*'`. Migrating a legacy hub that was open to all signed-in users carries that over as an "Everyone signed in" row, which keeps working. | Nothing is required. To move to named access, grant the people or groups who need the agent, then remove the "Everyone signed in" row (org admins). |
| Console user management: one **Administrator** role sets Hubzoid administration and the chat app's admin role together. **Delete user** (in a user's **…** menu) really deletes the account and its chats; Activity history, usage records and published artifacts are kept. The Console no longer offers Block or Reactivate. With the Open WebUI Users page hidden and every agent managed in the Console, Open WebUI's Groups page is hidden too. | Before relying on the Console, check People for users marked **Needs attention** (an administrator on only one side) and choose their role. Anyone blocked earlier stays blocked; delete them or unblock them through the management API ([blocked users](access-management.md#blocked-users)). |
| `identity/permissions.yaml` can no longer relabel built-in capabilities (Use this agent, Manage access, Save shared knowledge). Such entries are ignored with a warning. | Remove those entries if you had any. Labels for `restricted/` tools still work. |
| A personal Open WebUI MCP connection is used only by people granted that app's `connector_<app>` capability. | Grant `connector_<app>` to the people who use it. |
| Gateway: any bridge can serve the Console and the agent picker's access check. While one bridge is down or restarting, the edge tries the next one. This covers the Console and the picker check only. It is not high availability for the gateway or chat. | Nothing. |
| Usage is recorded per chat turn and workflow call (no message content). The Console shows usage totals above the agent cards. | Nothing. Counting starts at the upgrade. |
| The public port rejects `.` and `..` path segments and drops client-sent `X-Hubzoid-*` and `X-OpenWebUI-*` headers. | Nothing, unless you relied on those. |
| Docker: the old `docker/Dockerfile` is removed. The image is built from the `Dockerfile` at the repository root, now on Debian 13, and installs the checked-out source. Each release is also published as `ghcr.io/hubzoid/hubzoid:<version>` (amd64 and arm64). | Check out the release tag and run `docker build -t hubzoid .`, or pull the published image. `--build-arg HUBZOID_VERSION` no longer picks the version. For Compose, use `docker/docker-compose.yml` (SQLite) and add `docker/docker-compose.postgres.yml` for PostgreSQL. See [DEPLOYING.md](DEPLOYING.md#path-b-docker). |
| Docker: only the edge port 3080 is published. The bridge port 8000 is no longer published, because the bridge listens only inside the container. | Remove any `8000:8000` port mapping. Chat, downloads, the Console and MCP all go through port 3080. |
| Docker: the image's entrypoint is `hubzoid` with the default command `run /hub`, and it runs as an unprivileged user. The old Compose image ran as root. | If you passed arguments after the image name, pass the whole command now, for example `run /hub --slack`. On Linux, make the mounted hub folder writable by the container user ([DEPLOYING.md](DEPLOYING.md#path-b-docker)). |
| Hubzoid's own code is licensed under Apache-2.0. 0.9.x was MIT. Dependencies keep their own licences. | Nothing to run. If you redistribute Hubzoid, keep [LICENSE](../LICENSE) and [NOTICE](../NOTICE) with it. See [LICENSING.md](../LICENSING.md). |

### Steps

Before changing configuration, keep a protected copy of the current environment
files with the rollback materials. The default backup excludes secrets and `.env`.

1. **Set the bridge keys** in each hub's `.env` (see above).
2. **Stop** the hub, or the gateway and its bridges. Let running scheduled tasks
   finish first.
3. **Install** the new version in the same environment:
   ```bash
   pip install -U hubzoid
   ```
   With Docker, build or pull the new image instead, and run the `hubzoid`
   commands below with that image. Its entrypoint is `hubzoid`.
4. **Back up**, before the first start:
   ```bash
   hubzoid backup <hub> --out pre-upgrade.tar.gz
   ```
   Backup only reads, so this is a copy of the deployment exactly as the old
   version left it. Nothing is upgraded until the first start. For PostgreSQL,
   also `pg_dump` ([BACKUP.md](BACKUP.md)).
   With a gateway, 0.9.x hubs do not point at the gateway until its first start
   on this version, so this backup holds only the named hub's state and no
   accounts or chats. Run it for each hub, and with the gateway stopped also
   archive its `--data-dir` yourself, for example
   `tar czf pre-upgrade-gateway.tgz -C /srv gateway-data`.
5. **Check**:
   ```bash
   hubzoid doctor <hub>
   python -c "import sqlite3; print(sqlite3.sqlite_version)"
   ```
   Fix every `fail`. `db.operational` shows `info` or `warn` until the first
   start upgrades the schema.

   **Workflows on Python 3.12 need SQLite 3.42 or newer, or PostgreSQL.** The
   workflow engine (DBOS) runs markdown schedules and code workflows. On Python
   3.12 with an older SQLite it refuses to start, and doctor reports
   `deps.sqlite` as `fail`. Run the second command with the same Python the
   hub uses (the virtual environment's `python`, or the container's). Python
   3.11 is not affected. Hubzoid supports Python 3.11 and 3.12. To fix it, use a
   Python build with a newer SQLite (python.org, uv, Homebrew, Debian 13,
   Ubuntu 24.04) or PostgreSQL.
6. **Start** as before. At the first start Hubzoid upgrades its tables. It
   imports the old decision logs the first time it records or reads a tool
   decision. For an existing gateway deployment with independently managed
   bridges (`--no-bridges`), upgrade and restart the bridges first, verify their
   `/healthz` responses, then restart the gateway. Keep `MODEL_LABEL` consistent
   with each hub's configuration. The gateway accepts older health responses
   that omit the model and retries identity mismatches until its startup timeout;
   it still refuses a different hub or a conflicting reported model.

   On a first deployment without a manifest, start the gateway to record it,
   then start/restart the bridges while the gateway waits for their health
   checks. See [deployment lifecycle](DEPLOYING.md#multi-hub-on-one-address-hubzoid-gateway).
7. **Verify**: sign in as a normal user and chat with each agent, download a
   file the agent makes, open the Console (Agents, per-agent Runs & schedules, Activity), and run
   `hubzoid doctor <hub>` again.

### Going back

Stop everything. Restore with the new version still installed (older versions
have no `restore` command), then reinstall the previous version and start:

```bash
hubzoid restore pre-upgrade.tar.gz
pip install "hubzoid==<previous version>"
```

For a gateway, also put back the `--data-dir` archive from step 4 while
everything is stopped: Open WebUI's database is upgraded at the first start too.

Restore the matching pre-upgrade environment configuration too, especially if
bridge keys or connection settings changed. Database state and credentials must
agree on both sides of the chat-to-bridge connection.

Whatever the restore replaces is kept beside it as `<name>.pre-restore-<time>`.

Restore deliberately refuses a SQLite database with WAL/SHM sidecars. After an
unclean shutdown, stop every process using that database and preserve the database
**together with its sidecars**. Checkpoint the stopped database with SQLite, then
close that connection and retry restore:

```bash
sqlite3 /path/to/database.db 'PRAGMA wal_checkpoint(TRUNCATE);'
```

Do not delete sidecars to force a restore: they may contain committed data. If you
cannot confirm that all writers have stopped, restore to a separate location with
`--move` as described in [BACKUP.md](BACKUP.md). The current standalone and gateway
supervisors wait for their child services during normal shutdown.

### First sign-in and usage after the upgrade

The configured, verified owner now receives initial administration and hub entry
once. Existing hubs are not automatically migrated to managed access. Record the
owner email before rollout, back up all stores, and verify owner and ordinary-user
access after restart. Subsequent sign-in preserves revocations.

The bridges serve the Console, so they must know the owner. With `--no-bridges`,
the gateway records its `HUBZOID_GATEWAY_ADMIN_EMAIL` and public address in the
deployment manifest at its first start, and the bridges read them after the
one restart in step 6. Until that restart the Console refuses every account.

New workflow scaffolds are manual and model-free. Existing schedules are unchanged.
Chat usage now uses the forwarded chat ID; local Open WebUI background task types
are recorded separately from user messages. Historical usage is not rewritten.

### Only if you ran a pre-release build of 1.0.1

Skip this section if you are upgrading from 0.9.x. Those releases had no
workflow engine and no code workflows, so there is no run history or queued run
to carry over.

If you ran a pre-release build of 1.0.1, also note:

- The Console's **Add person** is now **Add user**, and its Public access switch
  is removed. `POST /portal/api/accounts` keeps an account whose grants failed
  (502 `partial`), so treat `partial` as "account exists without access" and
  retry the grant. New `workflow:*` identities can't be added in the Console.
  The Console's service account reuses its Open WebUI token.
- In code workflows, `hub.call_llm` is now one model call without tools. Pre-release
  builds ran the full agent there. If a workflow needs tools there, use
  `hub.call_agent`.
- Code-workflow agent calls are not retried unless `agent_max_attempts` is set.
  Runs left over from older workflow code are cancelled at start. A markdown run
  that had not started yet is queued again under the new code first. Drain
  queued runs before upgrading ([workflows.md](workflows.md)).
- Pre-release builds used an older version of the workflow engine. Carrying their
  run history across is not part of this release. Keep the old workflow database
  (`.hubzoid/dbos.db` by default) and rehearse the upgrade on a copy first. If a
  deployment needs that history and the new version cannot read it, treat the
  rollout as failed and go back. Deleting the old database is not a migration.

### Workflows run as people; artifacts and email

Scheduled work now acts as an ordinary account, and can publish private
artifacts (reports, PDFs, CSVs and other files) and email its owner ([workflow-identity.md](workflow-identity.md),
[reports-and-email.md](reports-and-email.md)). The database gains the tables for
this at the first start (`op_0005`, forward only: restore a backup to go back).

| Change | What you do |
|---|---|
| Runs act as `run_as`, else `HUBZOID_WORKFLOW_USER` (hub, then deployment), else the owner recorded at setup, instead of `workflow:<name>` / `workflow:md:<task>`. | Run `hubzoid schedule list <hub>`: it shows who each workflow and task runs as. Set `run_as` or `HUBZOID_WORKFLOW_USER` if the default is not the account you want. |
| Grants to `workflow:*` subjects are kept but no longer used. They are never copied to anyone. | For each permission a run's log reports as "not held", grant it to the account the work runs as (or point `run_as` at an account that holds it), then remove the old grant. |
| `hub.state` belongs to the account a run acts as, and each account's state starts empty. State written before the upgrade is kept, untouched and assigned to no one. A markdown task that runs as a person likewise gets its own scratch folder, `.hubzoid/schedule/<task>@<person>/`, and starts with no state file; the old `.hubzoid/schedule/<task>/` is kept as it was. | Expect such a task's first run as a person to begin from scratch (for example, reprocess its first window). To carry state over deliberately, copy the old state file into the person's folder before that run. For state that must survive a change of account, use `hub.shared_state` (keep personal data out of it). |
| The Console's run history shows a hub's managers each run's workflow, status, timing and a failure summary. It shows the run's result and step outputs only to the account the run acted as, or for legacy service runs. Runs recorded before the upgrade carry no identity, so they show metadata only. | Nothing. On the server, `hubzoid schedule status` still shows everything, and each markdown run's log is under `.hubzoid/schedule/`. |
| New permission **Share artifacts publicly** (`share_public_links`): anyone with such a link can open the artifact without signing in. Publishing an artifact never grants it. | Grant it only to people who may share their artifacts with anyone who has the link. |
| Grants record when they were made (`op_0007`, forward only). Removing `share_public_links` from someone (or blocking them) ends their public links for good; granting it again does not revive them. Grants from before the upgrade have no time and their links keep working. | Nothing. |
| `HUBZOID_WORKFLOW_USER` in a hub secret now wins over the same key in `<hub>/.env`, as documented. `hubzoid schedule list` and the Console's **Runs as** show the account runs actually use. | If a hub sets the key in both places with different accounts, keep the one you mean and remove the other. |
| Email preview files (`HUBZOID_EMAIL_DELIVERY=preview`) are created readable only by Hubzoid's account (files 0600, folders 0700). Files written earlier keep their permissions. | Optionally `chmod -R go-rwx <hub>/.hubzoid/outbox`. |
| Owner email needs `HUBZOID_SMTP_HOST` and `HUBZOID_SMTP_FROM` (plus credentials, over TLS). | Set them in the deployment configuration (or a deployment secret) when you want workflow email. Use `HUBZOID_EMAIL_DELIVERY=preview` to try it without sending. |
