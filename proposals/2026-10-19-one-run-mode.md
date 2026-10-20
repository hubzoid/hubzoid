# One run mode, and one home for every setting

Status: proposed for 2.0, revision 2 (20 Oct 2026). Revision 1 (19 Oct) was
milestone A's draft of section A. This revision adds the founder's direction of
20 Oct ("the gateway was a mistake, it should have been simple always"), one
home for every setting (B) and the database cleanup (C). Nothing here is built
until the founder approves it.

It replaces, if approved:

- revision 1's separate units per hub (`--no-bridges`, `hubzoid bridge`);
- in `2026-10-19-hosted-data-dir-and-deploy.md`, "a gateway refuses
  `HUBZOID_DATA_DIR`" and "a channel's active configuration is its revision in
  the database";
- publishing a brand (`docs/branding.md`), applying chat defaults
  (`docs/chat-config.md`) and Workspace drafts (`hubzoid/chat/authoring.py`).

## Problem

### Two ways to start

- `hubzoid run <hub>` starts one bridge and the web app. Its settings live in
  the hub's `.env`, its store is the hub's own `.hubzoid/hub.db`, and its routes
  have no `/b/<slug>` prefix.
- `hubzoid gateway <hub>...` starts an edge, one bridge per hub, a shared
  store and a manifest. Deployment settings live in the gateway's environment,
  and hub `.env` files are read again for compatibility.

A fix in one mode leaves the other behind. The 2.0 review on a reference
deployment found five bugs that exist only because there are two modes:

- **B2 / F10.** Behind a gateway, `/v1/chat/completions` always answered as
  the first hub, so a personal API key limited to any other agent could never
  be used. (Milestone B added `/b/<agent>/v1`.)
- **F16.** Approval and "Waiting for you" links were built from
  `HUBZOID_PUBLIC_URL`, which behind a gateway holds `/b/<hub>`, so they
  pointed at a path the edge doesn't route.
- **U17.** `hubzoid admin create --link` printed `http://127.0.0.1:3080/…` for
  a gateway on port 3750, because the CLI didn't know it was in a gateway.
- **W48.** The Brand tab said "Using Hubzoid branding" while the gateway showed
  the first hub's `branding/` folder.
- **U9.** Every bridge printed the same bridge-key warning.

### One broken hub stops all of them

- `hubzoid gateway` exits when any child exits (`_wait_any` in
  `hubzoid/cli.py`), and a bridge that fails to start stops the whole start
  ("fix that hub, then start the gateway again"). A broken `tools_local` file
  in one hub takes every agent down.
- So the reference deployment runs a systemd unit per hub
  (`hubzoid run <hub> --no-ui [--slack] --bridge-port 800X`), a unit per
  inbound server (`hubzoid inbound run <hub>`, because the gateway doesn't
  start one), and `hubzoid gateway … --no-bridges`. Restarting one agent is
  `systemctl restart hubzoid-<hub>`. That works, but it is the second mode
  again: every unit repeats ports and flags, and adding a hub means editing the
  gateway unit's command line too.

### Channels are flags

There are three ways to turn a channel on, and they disagree:

- `hubzoid run --slack | --whatsapp | --telegram | --webhook` starts the 1.x
  Slack app and inbound server as extra processes.
- `hubzoid gateway` has no such flags. It routes `/webhooks/<hub>` to an
  inbound server when a hub's `.env` holds `WHATSAPP_*`, `TELEGRAM_*` or
  `WEBHOOK_INBOUND_*` (`gateway._inbound_enabled`), but never starts that
  server or the Slack app. Only a hand-written unit with the flag does.
- 2.0 channels are files (`channels/<id>.yaml`) that take effect only after
  `hubzoid channels apply` copies them into the database.

Webhooks arrive at two address shapes: `/webhooks/<hub>/<name>` (1.x inbound
and workflow events) and `/api/hubs/<key>/channels/<id>/webhook` (2.0
channels).

### Settings live in two places

| Setting | In the folder | Also in the database | What goes wrong |
|---|---|---|---|
| Brand | `branding.yaml`, `branding/` | 8 `hz_branding_*` tables: drafts, reviews, revisions, activations, assets | A folder edit does nothing until a draft is reviewed and activated. Settings and the page disagreed (W48). |
| Chat defaults | `chat/config.yaml` | `hz_chat_config_heads`, `_revisions` | A file edit needs `apply`. A Settings edit makes a revision the file doesn't have. |
| Diagnostics limits | `admin.yaml` | `hz_admin_config_plans`, `_heads`, `_revisions` | Same plan and apply step. |
| Agent instructions | `AGENTS.md`, `skills/`, `prompts/` | Workspace drafts and versions (`hz_authoring_*`) and hub changes (`hz_hub_changes`) | Two review flows for the same files, both of which commit. |
| Connectors | `connectors/.mcp.json` | Console connectors (`hz_connectors`, `hz_connector_agents`) | A connector added in Settings is invisible to the folder, Git and Claude Code. |
| Channels | `channels/<id>.yaml` | `hz_channel_connections.config_json` | The running copy can differ from the file. |
| Cards | `cards/*.yaml`, `mcp-apps.yaml` | `hz_app_config_revisions` | Same. |
| Secrets | `.env`, `restricted/.env`, AWS | `hz_secrets` | Two places to look. The file wins and the stored value is kept but unused. |
| Phone numbers | `identity/access.csv` | `hz_identities.phone` (Settings) | Two lists of who a WhatsApp number is. |

Each pair has its own apply step and its own rule for which copy wins.

## Why now

- 2.0 goes to production next, and the reference deployment moves to it.
  Every later fix to "which mode am I in" or "which copy wins" costs an
  upgrade step for a live deployment.
- Every 2.0 feature (accounts, keys, approvals, workflows, channels) builds on
  the deployment shape. It should be one shape before more features add their
  own "if gateway" branches.
- The database tables behind the duplicates are new in 2.0. Release 1.3.2
  stops at migration `op_0019`; `op_0020` to `op_0071` have never shipped. They
  can be removed now without moving anyone's data. After launch, each one needs
  an export path.

## What other tools do

Read from the sources in `HubzoidAgent/RawTools` (paths below are inside it)
and the briefs in `HubzoidAgent/ToolsAnalysis`.

| | eve | Agno AgentOS | Mastra |
|---|---|---|---|
| Verbs | `eve dev`, `build`, `start`, `deploy` (`eve-master/eve-main/docs/reference/cli.md:8-23`) | Python: `AgentOS(...).serve()` (`agno-main/libs/agno/agno/os/app.py:2426`) | `mastra dev`, `build`, `start`, `deploy` (`mastra-main/packages/cli/src/index.ts:128-262`) |
| Many agents | One `eve start` process per agent, each on its own port behind a proxy (`docs/guides/deployment/self-hosting.md:64-96`) | One FastAPI app for every agent, team, workflow and interface (`os/app.py:278-296`) | One server bundle for every agent; separate worker processes chosen by name (`index.ts:199-247`, `commands/worker/start.ts:48`) |
| Restart one agent | Only by running each agent as its own service. "Process restarts … remain hosting responsibilities" (`docs/guides/frontend/nextjs.mdx:206`, `self-hosting.md:19`) | No. `serve(reload=True)` restarts the whole app (`os/app.py:2478-2490`); `resync()` re-reads agents in place, but exposed MCP tools need a full restart (`os/app.py:598-645`) | No. `mastra start` exits when the server exits (`commands/start/start.ts:72-85`); `mastra dev` restarts the whole server after an error, at most 3 times (`commands/dev/dev.ts:35, 282-300`) |
| In-flight work across a restart | Dev keeps immutable runtime generations: running turns keep their code, new turns get the rebuild (`cli.md:289`). `eve start --worker-only` keeps an old build finishing its runs (`research/stranded-session-revival.md:141-153`) | Durable runs checkpoint to the database (`ToolsAnalysis/agno.md:78`) | After a dev restart, asks the server to restart active workflow runs (`dev.ts:86-100, 237`) |
| Channels | A file per channel, `agent/channels/slack.ts`; secrets `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET` from the environment (`docs/channels/slack.mdx:19-39`) | Interfaces passed in code (`interfaces=[Slack(agent)]`); the signing secret from the environment (`os/interfaces/slack/security.py:9`) | — |
| Settings | Files in the agent folder; secrets in `.env.local` or the host's environment | One YAML file for UI and chat settings, quick prompts per agent (`os/config.py:521-559`, `os/utils.py:2123`) | Loads `.env.production`, `.env` (`start.ts:31`). Studio edits to code-defined agents are written as committed files, never `model` or `name`, which "could drift from the source-of-truth declaration in code" (`packages/core/src/storage/domains/agents/filesystem.ts:35-37`) |
| Path-mounted agents | Need `EVE_PUBLIC_ROUTE_PREFIX` so callback links use the public path (`self-hosting.md:100`), the same bug as F16 | — | — |

What we take:

- **From eve:** files turn features on (channels, schedules, evals), secrets
  come from the environment, and the verbs stay plain. Running turns finish on
  the code they started with while new turns get the new code.
- **From Agno:** one HTTP surface for many agents, and UI and chat settings in
  one YAML file.
- **From Mastra:** the folder is the source of truth. A tool may help edit a
  file, but it never keeps its own copy.
- **Our own:** a supervisor that restarts one hub alone. None of the three
  does this: Agno and Mastra restart everything, and eve leaves it to separate
  services. We need it because a Hubzoid deployment shares one sign-in and one
  store across agents, which eve's one-service-per-agent model does not.

## What should happen

### A. One run mode

**A1. One command.** `hubzoid run` starts the deployment: the edge on the
public port, one bridge process per hub, and each hub's channels, scheduler
and workflows.

- `hubzoid run` with no argument runs the deployment file in this folder
  (`hubzoid.yaml`), else the hub in this folder.
- `hubzoid run ./a ./b` runs these hubs with no file, like `hubzoid gateway`
  today.
- One hub is a deployment of one: the same processes, routes and code path.
- Kept: `--port`, `--host`, `--data-dir`, `--no-open`. Removed: `--no-ui`,
  `--bridge-port`, `--slack`, `--whatsapp`, `--telegram`, `--webhook`. For one
  release a removed flag stops the start with its one-line replacement.
- `hubzoid gateway` works for one release as an alias with a notice. It accepts
  its `--name` and `--public-url`, and prints the `hubzoid.yaml` that says the
  same, to paste. `--no-bridges` is refused with a link to the new runbook.

**A2. The deployment file.** `hubzoid.yaml` in the deployment folder (name
and place are open question 2):

```yaml
version: 1
name: Example Hubs
public_url: https://hubs.example.org
sign_in: true
owner: owner@example.org
hubs:                 # folders; the first also answers at /
  - operations-hub
  - finance/finance-hub
brand: {...}          # was branding.yaml (B1)
chat: {...}           # was chat/config.yaml (B2)
diagnostics: {...}    # was admin.yaml, if open question 3 says so
```

- Secrets and process settings go in `.env` next to it (`HUBZOID_SECRET_KEY`,
  `DATABASE_URL`, sign-in client secrets, mail) or the host's environment,
  which wins over `.env`.
- State goes in `.hubzoid/` next to it, or `--data-dir` / `HUBZOID_DATA_DIR`:
  the generated manifest (never edited by hand), the shared store, keys, logs
  and the control socket. An existing gateway passes its old data folder and
  nothing moves.
- With `HUBZOID_DATA_DIR` and several hubs, each hub's state goes under
  `<data>/hubs/<hub>/`. A one-hub deployment keeps today's layout.
- For one hub, the hub folder is the deployment folder, as today:
  `hubzoid.yaml` is optional, `.env` is the hub's, and state stays in its
  `.hubzoid/`.
- `HUBZOID_PUBLIC_URL`, `HUBZOID_AUTH` and `HUBZOID_ADMIN_EMAIL` still work
  when the file leaves the key out, because hosted providers often know the
  public URL only at deploy time. If both are set and differ, the start stops
  and names both.
- A hub's `.env` holds only that hub's settings. One that sets a deployment
  setting is refused if it disagrees (as today), and gets a notice to remove
  the line if it agrees.

**A3. Hubs are independent.**

- `hubzoid run` is a small supervisor. Each hub is its own child process
  group: its bridge, plus its 1.x channel processes if any (A6). The edge is
  another child.
- A hub that exits is started again, waiting 1 s, then doubling to 60 s. After
  5 failed starts within 10 minutes, it is marked **failed** and left stopped
  until `hubzoid start <hub>`. Its last error stays in `hubzoid status`.
- While a hub is down, the edge answers its routes with 503, "This agent is
  restarting" (or "unavailable" with the reason when failed) and
  `Retry-After`. Today it answers 502 "upstream unavailable". Deployment-wide
  routes (sign-in, the web app, Settings) are served by any running bridge, as
  the edge's fallbacks already do.
- The supervisor never ends because a hub ended. It ends on SIGTERM or SIGINT,
  stopping hubs first and the edge last, or when the edge can't bind its port.
- At start the edge comes up first. Hubs start at most 3 at a time
  (`HUBZOID_START_CONCURRENCY`), so a small machine isn't swamped; a hub that
  isn't ready yet answers "starting".
- A broken `tools_local`, a bad `.env` value or a missing package fails that
  hub only.
- The supervisor assigns loopback ports for bridges and 1.x inbound servers
  and records them in the manifest. A `BRIDGE_PORT` in a hub's `.env` is still
  honoured. Bridge keys are generated per deployment unless a hub sets
  `BRIDGE_API_KEYS`, which ends the shared `dev` key warning (U9).
- If the supervisor is killed, its children notice their parent is gone and
  stop, so nothing keeps holding a port. systemd or the container platform
  restarts the whole deployment.

**A4. Control commands**, over a control socket in the data folder (a Unix
socket with mode 0600; on Windows a loopback port with a token file):

- `hubzoid status [--json]`: per hub, its state (starting, ready, draining,
  restarting in N s, failed, stopped), uptime, restarts, last exit, channels,
  and "restart needed" with the reason.
- `hubzoid restart <hub> [<hub>...]`, `hubzoid stop <hub>`,
  `hubzoid start <hub>`, `hubzoid reload [<hub>]`, `hubzoid logs <hub> [-f]`.
- Hubs are named by folder name (`irs-hub`) or slug. The commands find the
  deployment from the current folder or `--data-dir`, and say plainly when
  nothing runs there.
- Anyone who can open the socket file can use them: the service's user and
  root. There is no network access.
- **A restart drains first.** The hub takes no new turns (a new message gets
  "This agent is restarting, try again in a moment"), and running turns get up
  to `--grace` seconds (default 120). Then the process stops and starts.
  - Scheduled runs don't fire while it drains. Channel deliveries are recorded
    and routed after the start, as they are today.
  - A turn still running at the deadline ends as interrupted. The chat engine
    already records that and tells the person.
  - Durable workflow runs (DBOS) and approvals waiting for a person resume after
    the start, as they do after any restart today.
- Example: with 11 hubs, `hubzoid restart irs-hub gpms-hub` restarts only those
  two, one after the other.

**A5. Reload instead of restart where possible.**

| Change | Takes effect |
|---|---|
| `AGENTS.md`, skills, knowledge, `hitl.yaml` | On the next turn, as today |
| `brand`, `chat`, `diagnostics` in `hubzoid.yaml`; `channels/`, `connectors/`, cards | `hubzoid reload` |
| The hub list in `hubzoid.yaml` | `hubzoid reload` starts new hubs and drains removed ones |
| `.env`, `tools_local/`, `restricted/`, new packages, `sign_in`, `public_url` | A restart: `hubzoid restart <hub>`, or the service for deployment settings |

- Reload validates first. An invalid file changes nothing, and `reload` exits 1
  naming the file and line.
- Reload uses the bridge's existing runtime reloader (the one published
  instructions use): new turns get the new runtime and running turns finish on
  the old one, like eve's runtime generations.
- `hubzoid status` compares what each hub started with (digests of `.env`,
  `tools_local/`, `restricted/` and requirements) with the folder, and says
  "restart needed: .env changed". Nothing is silently stale.
- No file watcher in this release.

**A6. Channels by file.**

- A channel is on when its file in the hub's `channels/` folder says
  `enabled: true`. Its secrets come from `.env` (or the secret store, see B8).
  There are no flags.
- 2.0 connections (Slack, GitHub, Linear, email, WhatsApp Cloud) are files
  already. The file becomes the only copy: start and `hubzoid reload` read it,
  and `hubzoid channels apply` becomes "validate, then reload this hub".
- The 1.x surfaces get a file each, so they need no flag. Their code and
  behaviour don't change:

  ```yaml
  # channels/slack-assistant.yaml
  version: 1
  id: slack-assistant
  provider: slack_assistant   # 1.x Slack app, Socket Mode: SLACK_BOT_TOKEN, SLACK_APP_TOKEN
  enabled: true
  ```

  `provider: whatsapp_inbound`, `telegram` and `webhook` do the same for the
  1.x inbound server (`WHATSAPP_*`, `TELEGRAM_*`, `WEBHOOK_INBOUND_*`). The
  supervisor starts them as part of their hub, so restarting a hub restarts its
  channels.
- Moving the reference deployment's Slack app to 2.0 Slack connections is not
  part of this. 1.x Slack finds a person by their Slack profile email, while
  2.0 connections need an account link per person. That is a product change of
  its own.
- For one release, a hub whose `.env` has the 1.x variables but no channel
  file still starts that surface, and every start names the file to add. After
  that release, the file is the only switch.
- `hubzoid doctor` and `hubzoid status` name every channel whose file is
  invalid or whose secrets are missing. That channel stays off and the hub
  runs.
- Webhooks all arrive on the one public port. The documented address becomes
  `/b/<hub>/webhooks/<channel-id>` for every kind. Existing addresses
  (`/webhooks/<hub>/…` and `/api/hubs/<key>/channels/<id>/webhook`) keep
  working, because providers have them registered.

**A7. Addresses.**

- Links (set-password, approvals, waiting-for-you, shares, downloads) are
  built from the deployment's public origin, never from a hub's path.
- Each hub has `/b/<slug>/…`: `api`, `v1` (personal API keys), `mcp`,
  `artifacts`, `branding` and webhooks. The first hub also answers at `/`, so
  one-hub URLs and bookmarks keep working.
- Warnings about the deployment are printed once by the supervisor, naming the
  hubs they concern.

**A8. Production.**

- `hubzoid service install` writes one systemd unit, `hubzoid.service`, that
  runs `hubzoid run` in the deployment folder with `Restart=on-failure`. It
  prints the file (or writes it with `--write`) and the two commands to enable
  it. Single hubs restart with `hubzoid restart`.
- Containers (Railway, Maritime, Docker): one container runs `hubzoid run`, and
  the platform restarts the container if the supervisor exits.
- Separate units per hub are not offered in this release (open question 4).
  `hubzoid restart` covers the reason people wrote them.

**A9. Upgrade.**

- A single hub: `hubzoid run .` keeps working with its state where it is (its
  `.hubzoid/hub.db` is the shared store). It gains the `/b/<slug>/` routes.
- A gateway: `hubzoid gateway …` works for one release (A1). To move, write
  `hubzoid.yaml` (the alias prints it) and start `hubzoid run --data-dir <old
  data folder>`.
- A hub that ran alone and later joins a multi-hub deployment needs its
  accounts and chats merged into the deployment's store. That is a separate,
  explicit command (`hubzoid deployment add <hub>`), never automatic, and not
  part of this work.
- The reference deployment: one unit replaces the unit per hub, the inbound
  units and the gateway's. These lines change:

| Today | After |
|---|---|
| `hubzoid gateway <hubs> --no-bridges --name … --public-url …` (gateway unit), a unit per hub `hubzoid run <hub> --no-ui [--slack] --bridge-port 800X`, and `hubzoid inbound run <hub>` units | `hubzoid.yaml` with `hubs`, `name`, `public_url`; one `hubzoid.service` |
| IRS: `--slack` with `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` in its `.env` | `channels/slack-assistant.yaml`; the same two `.env` lines |
| Nurturing: `WHATSAPP_*` in its `.env`, served by an inbound unit | `channels/whatsapp.yaml` (`provider: whatsapp_inbound`) |
| Support: `WEBHOOK_INBOUND_SECRET`, `WEBHOOK_INBOUND_NAME` | `channels/<name>.yaml` (`provider: webhook`) |
| `HUBZOID_AUTH`, `ENABLE_SIGNUP`, `DEFAULT_USER_ROLE`, `BYPASS_MODEL_ACCESS_CONTROL`, `WEBUI_SECRET_KEY`, `WEBUI_NAME`, `HOST`, `LICENSE_KEY` in hub `.env` files | Delete: `sign_in` and `name` live in `hubzoid.yaml`; the rest are retired 1.x names |
| `BRIDGE_PORT`, `BRIDGE_API_KEYS` in hub `.env` files | Optional; delete to let the supervisor assign them |
| `HUBZOID_ADMIN_EMAIL` in the gateway's systemd drop-in | `owner:` in `hubzoid.yaml` |
| `gateway-data/branding/` | `branding/` in the deployment folder |

  The rewritten runbook section (one unit, `hubzoid restart`, the table above)
  is delivered to the deployment's owner as a file. This work doesn't edit that
  repository.

### B. One home for every setting

**The rule.** Each setting has one home:

- **The folder** (the hub folder, and the deployment folder for deployment
  settings) holds everything a builder writes: instructions, knowledge,
  skills, tools, channels, connectors, cards, approval rules, brand and chat
  defaults. It is versioned with Git.
- **`.env`** (or the host's environment) holds secrets and process settings.
- **The database** holds what people and runs create: accounts, access, keys,
  sessions, chats, files, tokens, approvals, runs, audit and history.

Nothing lives in two of these. A file is read at start and at
`hubzoid reload`. The database never keeps an "active copy" of a file, and
Settings never writes one.

**A broken file turns off only what it describes, and says so.** An invalid
brand or chat section shows product defaults; an invalid connector or channel
file leaves that connector or channel off. Doctor, `hubzoid status` and
Settings name the file, the line and the reason. There is never a fallback to a
stale copy.

| Setting | Home | Settings shows | The database keeps |
|---|---|---|---|
| Brand | `brand:` in `hubzoid.yaml`, images and `theme.json` in `branding/` | Read-only, with the file to edit | Each person's acceptance of the terms and dismissed announcements |
| Chat defaults | `chat:` in `hubzoid.yaml` | Read-only | Each person's own appearance choices |
| Diagnostics limits | `diagnostics:` in `hubzoid.yaml` (open question 3) | Read-only | Diagnostic runs and support bundles |
| Agent instructions | `AGENTS.md`, `agents/`, `skills/`, `prompts/`; history in Git | Edits go through hub changes (B4) | Hub change proposals and decisions |
| Connectors | `connectors/<id>.yaml` and `connectors/.mcp.json` | Read-only, plus "Connect my account" | Each person's tokens, and clients registered with a provider |
| Channels | `channels/<id>.yaml` | Read-only, plus status, deliveries, threads and account links | Deliveries, threads, links, outbox, provider cursors |
| Cards | `cards/*.yaml`, `mcp-apps.yaml` | Read-only | Card instances and the card pages already shown |
| Hub setup values | `hub.settings.yaml` (already one file, written by the Setup tab) | Editable, writes the file | Nothing |
| Secrets | `.env`, `restricted/.env`, the host, AWS (open question 1) | Names and status, never values | Depends on open question 1 |
| Access, people, keys, MFA, sessions, company sign-in, workflow access | Database | Editable, as today | All of it |
| Chats, files, personal sources, personal memory (labelled Beta), repeated requests, runs, approvals, audit | Database | As today | All of it |

**B1. Brand.**

- The deployment's brand is the `brand:` section of `hubzoid.yaml` (today's
  `branding.yaml` schema, unchanged) with its images and `theme.json` in
  `branding/` next to it. A hub's own `branding/` gives that agent's avatar,
  as today.
- Draft, review, preview, activate and roll back are removed, with their
  routes, CLI and 6 tables. History is Git history.
- Settings → Brand shows what people see and where it comes from: "Edit
  `hubzoid.yaml` (brand) and `branding/`, or ask Claude Code or Codex to change
  it. Then run `hubzoid reload`."
- Images are served from the folder with a digest in the URL, so caching stays
  safe.
- Each person's own appearance choice (light, dark, text size) stays theirs.
- For one release, a deployment with no `brand:` section reads
  `branding.yaml`; doctor says to move it.

**B2. Chat defaults.** The `chat:` section of `hubzoid.yaml` holds what
`chat/config.yaml` holds today, with the same schema. Apply, revisions,
`export` and `set` are removed with their 2 tables. Settings shows the
defaults read-only. Personal preferences (`hz_chat_preferences`) stay. For one
release `chat/config.yaml` is read when the section is missing.

**B3. Diagnostics limits.** If open question 3 says file-only, `admin.yaml`'s
four limits move into `diagnostics:` and the plan and apply step goes, with 3
tables. Diagnostic runs and support bundles stay in the database.

**B4. Agent instructions and their history.**

- Workspace drafts (`hubzoid/chat/authoring.py`) and hub changes
  (`hubzoid/hub_changes/`) both propose, approve and commit edits to the same
  files. Keep one: hub changes, which already commits with the proposer and
  approver in the trailers.
- Hub changes learns the two paths only the Workspace covers today:
  `agents/<name>/AGENTS.md` and `prompts/<id>.md`.
- Published versions and rollbacks come from Git: a rollback proposes the file
  as it was at an earlier commit.
- `hz_authoring_drafts` and `hz_authoring_versions` go. Personal prompts
  (`hz_personal_prompts`) stay.
- A hub that isn't in a Git repository has no history; doctor says so.

**B5. Connectors.**

- A connector's definition lives in the hub that uses it,
  `connectors/<id>.yaml`: name, URL, how people sign in (`oauth` per person,
  `shared` company key, or `none`), scopes, the tool allow-list, whether writes
  need approval, and whether it is restricted.
- Its secrets (a pre-registered OAuth client secret, a shared company key) are
  named by reference (`client_secret: env:GITHUB_MCP_CLIENT_SECRET`), never
  written in the file.
- `connectors/.mcp.json` stays as it is, for servers the runtime starts or
  calls with hub-wide credentials. It is Claude Code's own format, which
  doesn't describe per-person sign-in.
- The database keeps only what people and providers create: each person's
  tokens (`hz_connector_tokens`), Hubzoid's own MCP sign-ins (`hz_mcp_oauth`),
  sign-in flows in progress, and clients registered with a provider
  dynamically.
- Settings shows connectors read-only, with "Connect my account" for each
  person.
- Console connectors came in 1.2, so released deployments may have some.
  `hubzoid connectors export` writes each one into the hub folders that use it
  and moves its secrets (B8). A deployment with Console connectors that haven't
  been exported keeps them working for one release, marked "move to the
  folder", and doctor names them.

**B6. Channels.** The file is the only copy (A6).
`hz_channel_connections` keeps the status, the pause switch and provider
state, and stops storing the configuration. `hubzoid channels export` is
removed. With `HUBZOID_DATA_DIR`, a channel is set up by committing its file,
like eve's `agent/channels/*.ts`.

**B7. Cards.** `cards/*.yaml` and `mcp-apps.yaml` are read at start and
reload. `hz_app_config_revisions` goes. Card instances and the card pages
already shown (`hz_app_instances`, `hz_app_resources`, stored by digest) stay,
because a card someone already saw must keep rendering.

**B8. Secrets (open question 1).** Two choices:

- **(a) `.env` and the host's environment only.** Remove the store (`hz_secrets`,
  `hubzoid secrets`, its routes and Settings screen). References become
  `env:NAME` only. Settings shows each name's status and where it is set.
  Simplest, and the same as eve and Mastra. The cost: on a hosted provider with
  several hubs in one container, a per-hub key must go in that hub's `.env` on
  the volume, or in the provider's variables with a hub prefix. A key Hubzoid
  creates (a GitHub App key from the manifest flow) is written to a file in the
  data folder.
- **(b) Keep the store as it is, narrowed.** `.env` stays the default and wins.
  The store is for people who set keys from a browser with no file access, and
  for keys Hubzoid creates. Each name has one effective home and Settings says
  which. The cost: two places to look, and code to keep.

Recommendation: **(b)** until the first hosted multi-hub deployment shows
whether browser-set keys are needed, because removing working code that hosted
deployments may need is costlier than keeping it. If the founder prefers (a),
it removes about 1,400 lines (`hubzoid/secretstore/`), its uses in 12
modules and one table, and the connectors export
writes secrets to `.env` instead of the store.

**B9. `identity/access.csv`.**

- Hubzoid stops reading it. Today it does two things: it gives labels (groups)
  to people, which grant nothing since 1.1, and on WhatsApp and Telegram it
  maps a number to a person when the hub has no `identity/access.py`.
- Phone numbers are recorded on people in Settings, which inbound channels
  already check first.
- 2.0 channels' `allowed_groups` (who may start work from a channel) used
  those labels. It is replaced by the person's access to the agent in
  Settings, which channels already check as well. A file with
  `allowed_groups` loads with a notice that it is ignored.
- `identity/access.py`, a hub's own code to look a number up in its systems,
  stays. The reference deployment's one roster reads its own CSV from inside
  `access.py`, so it doesn't change.
- Doctor names a hub that still has `identity/access.csv`.

### C. Database cleanup

The operational store is one database with 148 tables and one migration head.
That design stays. The per-hub DBOS file also stays, because each hub
recovers its own workflows when it restarts on its own.

**C1. Tables that go.**

| Tables | Why | First shipped |
|---|---|---|
| `hz_branding_drafts`, `_reviews`, `_revisions`, `_activations`, `_active`, `_assets` (6) | B1. `hz_branding_legal` and `_dismissals` stay: they are each person's | 2.0 (`op_0021`) |
| `hz_chat_config_heads`, `_revisions` (2) | B2. `hz_chat_preferences` stays | 2.0 (`op_0020`) |
| `hz_admin_config_plans`, `_heads`, `_revisions` (3) | B3, if file-only. Diagnostics tables stay | 2.0 (`op_0022`) |
| `hz_authoring_drafts`, `_versions` (2) | B4 | 2.0 (`op_0031`) |
| `hz_app_config_revisions` (1) | B7 | 2.0 (`op_0054`) |
| `hz_parity_candidates`, `_freezes`, `_reviews` (3) | Internal release tooling, not a product feature. The code and the Settings tab leave the package | 2.0 (`op_0023`) |
| `hz_connector_agents` (1), and the definition columns of `hz_connectors` | B5. The table keeps only dynamically registered clients | 1.2 (`op_0012`, `op_0016`) |
| `hz_channel_connections` configuration columns | B6 | 2.0 (`op_0070`) |
| `hz_shares` (1) | Pre-2.0 read-only links. They can't be made any more, and the 2.0 links have previews and audiences. Doctor counts them before the upgrade; owners share again | 1.x (`op_0010`) |
| `hz_secrets` (1) | Only if open question 1 is (a) | 2.0 (`op_0060`) |
| `hz_inbound_history` in each hub's `hub.db` | Moves into the operational store with a hub column (C3) | 1.x (hub store) |

`hz_chat_operations` stays: despite its name it is the shared idempotency
table for every control route.

**C2. Kept on purpose.**

- `hz_run_identities.legacy` marks runs moved from Open WebUI whose owner
  couldn't be proven. Dropping it would lose that audit fact.
- `hz_identity_attrs` holds per-hub attributes of a person that workflows read
  (`hubzoid/workflows/context.py`). Only tests write it today. It is decided
  with C4.

**C3. The per-hub `hub.db`.** Behind a gateway it holds one table: WhatsApp
and Telegram history (`hz_inbound_history`). Moving that into the operational
store, keyed by hub, leaves each hub with the deployment's store and its own
DBOS file. A one-hub deployment already uses one file for both, so nothing
changes there. The 1.x history is copied once at the first start.

**C4. Identities: one account table, then sign-in bindings.** Today:

- `hz_users`: the account (email, password, role, status).
- `hz_user_identities`: company sign-in bindings (issuer and subject).
- `hz_directory_accounts`: directory provisioning (SCIM) bindings.
- `hz_identities`: the access subject, with email, `owui_id` (now the account
  id for 2.0 accounts), phone and a pending flag. This overlaps `hz_users`.
- `hz_identity_attrs`: see C2.

The target: `hz_users` is the one account table, with the phone and pending
invitations on it, and the two binding tables point at it. `hz_identities` is
folded into it and `owui_id` is renamed `account_id` on the way.

This touches sign-in and access in about a dozen modules. Recommendation: do it
**after** the 2.0 launch, as its own migration with its own proof, not in this
work (open question 5). This work renames nothing in identity.

**C5. How the migrations run.**

- New tables and columns come in add-only migrations. One forward cleanup
  migration at the end of this work drops what C1 lists, with
  `DROP … IF EXISTS`, so 2.0 development databases that have them and 1.3.2
  databases that never had some both arrive at the same head.
- Migrations `op_0020` to `op_0071` stay as they are, because development
  databases have run them. No revision id is ever reused (lesson F11).
- Before the cleanup runs, any data that has a new home is moved or the start
  stops: Console connectors (B5) and 1.x inbound history (C3). Pre-2.0 share
  links are counted and dropped (C1).
- Tests upgrade a 1.3.2 database and a 2.0 development database (the reference
  copy, stamped `op_0071`) to the new head on SQLite and PostgreSQL, and check
  that people, chats, access, approvals and runs survive.

## Scope and non-goals

- No change to a hub's required files. Every new file is optional.
- No hosting-provider work (Railway, Maritime). One container running
  `hubzoid run` makes it simpler later.
- No new Settings screens. Brand, chat defaults, connectors, channels and cards
  become read-only views; instruction edits use the existing hub changes card.
- Not merging the stores of hubs that were run apart (`hubzoid deployment
  add`, A9).
- Not moving 1.x Slack, WhatsApp, Telegram or webhook users to 2.0 channel
  connections (A6).
- Not the identity merge (C4), unless the founder wants it now.
- No file watcher, and no separate units per hub (open question 4).
- Model defaults don't change: `claude-local` uses Claude Code's default and
  `codex-local` uses Codex's.

## Open questions for the founder

1. **Secrets:** `.env` and the host's environment only (B8 a), or keep the
   encrypted store as it is, narrowed (B8 b, recommended for now)?
2. **The deployment file:** `hubzoid.yaml` in the deployment folder
   (recommended: short and easy to find, next to the hub folders it lists), or
   another name or place (`deployment.yaml`, inside `.hubzoid/`)?
3. **`admin.yaml`:** file-only as `diagnostics:` in the deployment file
   (recommended), or keep its plan and apply step?
4. **Separate units per hub:** leave them out of this release (recommended:
   `hubzoid restart` covers the need and it is one code path), or ship
   `hubzoid@<hub>.service` and an edge unit as well?
5. **Identity merge (C4):** after launch as its own change (recommended), or
   in this work?

## Definition of done

- `hubzoid run ./hub` and `hubzoid run` with a two-hub `hubzoid.yaml` start
  the same processes in the same shape. `hubzoid gateway` is an alias with a
  notice.
- With 11 hubs running, `hubzoid restart` on 2 of them while chats stream on
  the other 9: the 9 streams finish, the 2 hubs drain and come back, and
  nothing else restarts.
- A killed bridge comes back on its own. Its routes answer "restarting"
  meanwhile, and the other agents keep answering.
- A broken `tools_local` file fails only its hub, which is marked failed with
  the reason, while the others serve.
- A restart during a waiting approval and during a running workflow: both
  resume.
- A channel webhook round trip on the shared port, turned on by a file alone.
- `hubzoid status`, `reload`, `logs`, `stop` and `start` work, and status
  says when a restart is needed.
- Brand, chat defaults, connectors, channels and cards each come from one file,
  Settings shows them read-only, and the C1 tables are gone after the cleanup
  migration.
- The five bugs in the Problem have regression tests that run on the one code
  path.
- Upgrades from 1.3.2 and from the 2.0 reference copy keep people, chats,
  access, approvals and runs.

## Proof

On the reference copy (all 11 hubs, 8 GB machine), in the browser, saved with
screenshots to `HubzoidAgent/factory/one-run-mode/proof.html`:

- the same checks in a one-hub and an 11-hub shape;
- restart 2 hubs while chats stream on the other 9;
- kill one bridge process and watch it come back alone;
- restart during a waiting approval and during a running workflow, and check
  both resume;
- break one hub's `tools_local` and check only that hub fails;
- a channel webhook round trip on the shared port.
