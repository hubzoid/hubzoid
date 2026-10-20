# One run mode, and one home for every setting

Status: proposed for 2.0, revision 2 (20 Oct 2026). Revision 1 (19 Oct) was
milestone A's draft of section A. This revision adds the founder's direction of
20 Oct ("the gateway was a mistake, it should have been simple always"), one
home for every setting (B) and the database cleanup (C), reconciled with a
Codex review (gpt-6.1-sol, high effort, 20 Oct; 24 findings, all checked
against the code, summarised at the end). Nothing here is built until the
founder approves it.

**Approved by the founder on 20 Oct 2026** with the recommended answers to the
open questions: secrets split by kind (B8 c), `hubzoid.yaml` in the deployment
folder, `admin.yaml` file-only, no separate units per hub, and the identity
merge after launch.

It replaces, if approved:

- revision 1's separate units per hub (`--no-bridges`, `hubzoid bridge`);
- in `2026-10-19-hosted-data-dir-and-deploy.md`, "a channel's active
  configuration is its revision in the database";
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
  stops at migration `op_0019`; `op_0020` to `op_0071` have never shipped, so
  outside development databases they hold no one's data yet. After launch,
  every one of them would need a migration path for real deployments.

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
public_url: https://hubs.example.org
sign_in: true
owner: owner@example.org
hubs:                 # folders; the first also answers at /
  - operations-hub
  - finance/finance-hub
brand: {...}          # was branding.yaml (B1); brand.identity.name is the deployment's name
chat: {...}           # was chat/config.yaml (B2)
diagnostics: {...}    # was admin.yaml, if open question 3 says so
```

- Secrets and process settings go in `.env` next to it (`HUBZOID_SECRET_KEY`,
  `DATABASE_URL`, sign-in client secrets and switches such as `ENABLE_SIGNUP`,
  mail) or the host's environment. As with a hub's `.env` today
  (`hubzoid/settings.py`), the file wins over the environment.
- State goes in `.hubzoid/` next to it, or `--data-dir`: the generated
  manifest (never edited by hand), the shared store, keys, logs and the control
  socket. **Existing state never moves.** An existing gateway passes its old
  data folder; each hub keeps its own state folder, DBOS file, inbound dedup
  markers, Telegram bindings and outputs where they are today.
- `HUBZOID_DATA_DIR` stays one hub per data folder, as the hosted proposal
  says. Several hubs in one hosted container is later work.
- For one hub, the hub folder is the deployment folder, as today:
  `hubzoid.yaml` is optional, `.env` is the hub's, and state stays in its
  `.hubzoid/`.
- `HUBZOID_PUBLIC_URL`, `HUBZOID_AUTH` and `HUBZOID_ADMIN_EMAIL` still work
  when the file leaves the key out, because hosted providers often know the
  public URL only at deploy time. If both are set and differ, the start stops
  and names both.
- A hub's `.env` holds only that hub's settings. Every deployment setting
  (the table below) is resolved by the supervisor before the edge opens, and a
  hub layer can never override it: a hub `.env`, `restricted/.env` or hub AWS
  secret that sets one is refused if it disagrees, and gets a notice to remove
  the line if it agrees. Today only sign-in and the deployment key are checked
  this way (`_gateway_mode_conflicts` in `hubzoid/cli.py`).

| Kind | Where | Precedence |
|---|---|---|
| Deployment settings: hub list, `sign_in`, `public_url`, `owner`, `brand`, `chat`, `diagnostics` | `hubzoid.yaml` | `HUBZOID_AUTH`, `HUBZOID_PUBLIC_URL`, `HUBZOID_ADMIN_EMAIL` fill a key the file leaves out; set in both and different stops the start |
| Deployment secrets and switches: `HUBZOID_SECRET_KEY`, database URLs, `OAUTH_*`, `GOOGLE_CLIENT_*`, `ENABLE_SIGNUP`, `ENABLE_LOGIN_FORM`, mail | Deployment `.env`, the deployment AWS secret | `.env` over the environment, the AWS secret over both, as today for a gateway |
| Hub settings: model, tool keys, `MCP_SERVER`, OTel | Hub `.env`, `restricted/.env`, hub AWS secrets | As today. `model:` in `AGENTS.md` overrides `.env` `MODEL`, as today |
| Process binding: port, host | `--port`, `--host`, `PORT`, `HUBZOID_HOST` | Flags over environment |

**A3. Hubs are independent.**

- `hubzoid run` is a small supervisor. Each hub is its own child process
  group: its bridge, plus its 1.x channel processes if any (A6). The edge is
  another child.
- A hub that exits is started again, waiting 1 s, then doubling to 60 s. After
  5 failed starts within 10 minutes, it is marked **failed** and left stopped
  until `hubzoid start <hub>`. Its last error stays in `hubzoid status`.
- While a hub is down, the edge answers its routes with 503, "This agent is
  restarting" (or "unavailable" with the reason when failed) and
  `Retry-After`. Today it answers 502 "upstream unavailable". The public text
  is neutral; the reason is only in `hubzoid status` and the logs.
- **Routes are classified.** Only deployment-wide routes (the web app, sign-in,
  Settings and the deployment APIs) may fail over to another bridge. Today
  every unmatched path does (`hubzoid/edge.py`), including bare
  `/v1/chat/completions`, so with the first hub down a personal key could get
  another agent's answer. The first hub's root aliases (`/v1`, `/mcp`,
  `/artifacts`, its webhooks) and every `/b/<slug>/…` route stay pinned to
  their hub and answer 503.
- The supervisor never ends because a hub ended. It ends on SIGTERM or SIGINT,
  stopping hubs first and the edge last, or when the edge can't bind its port
  at start. An edge that crashes later is started again like a hub; the public
  port is closed for those seconds.
- **Migrations run once, in the supervisor, before any child starts**, under
  the existing migration lock. Bridges no longer upgrade the shared store
  lazily on their own.
- One supervisor per deployment: it holds a lock file in the data folder for
  its lifetime, and refuses to start if another holds it.
- At start the edge comes up first. Hubs start at most 3 at a time
  (`HUBZOID_START_CONCURRENCY`), so a small machine isn't swamped; a hub that
  isn't ready yet answers "starting".
- A broken `tools_local`, a bad `.env` value or a missing package fails that
  hub only.
- The supervisor assigns loopback ports for bridges and 1.x inbound servers
  and records them in the manifest. A `BRIDGE_PORT` in a hub's `.env` is still
  honoured. A bridge key is generated once per deployment and kept in the data
  folder (mode 0600) unless a hub sets `BRIDGE_API_KEYS`; the supervisor hands
  it to the hub's bridge and its 1.x channel processes. This ends the shared
  `dev` key warning (U9).
- Each hub runs in its own process group and is stopped as a group. Some
  descendants start their own session (the Codex app-server,
  `hubzoid/factory_codex.py`), so the bridge stops those on shutdown, and in
  production the systemd unit's cgroup is the last net. If the supervisor is
  killed, its children notice their parent is gone and stop. systemd or the
  container platform restarts the whole deployment.

**A4. Control commands**, over a control socket in the data folder (a Unix
socket with mode 0600; on Windows a loopback port with a token file):

- `hubzoid status [--json]`: per hub, its state (starting, ready, draining,
  restarting in N s, failed, stopped), uptime, restarts, last exit, channels,
  workflow readiness (separately, because the workflow engine can wait for its
  previous owner's lease, `hubzoid/workflows/ownership.py`), and "restart
  needed" with the reason.
- `hubzoid restart <hub> [<hub>...]`, `hubzoid stop <hub>`,
  `hubzoid start <hub>`, `hubzoid reload [<hub>]`, `hubzoid logs <hub> [-f]`.
- Hubs are named by folder name (`sales-hub`) or slug. The commands find the
  deployment from the current folder or `--data-dir`, and say plainly when
  nothing runs there.
- Anyone who can open the socket file can use them: the service's user and
  root. The socket lives in a folder only that user can open; a stale socket
  is replaced only by the supervisor holding the lock. There is no network
  access. Commands for one hub run one at a time, and each names the hub's
  current generation, so a command never acts on a process started after it
  was sent.
- **A restart drains first, in this order:**
  1. The edge stops sending new webhooks to the hub (503, so WhatsApp,
     Telegram and generic senders retry).
  2. The hub's 1.x channel processes stop gracefully, finishing deliveries they
     already acknowledged while the bridge still accepts their turns. Today the
     1.x WhatsApp and Telegram paths acknowledge, claim a dedup marker and
     then process in memory (`hubzoid/inbound/harness.py`), so a delivery cut
     off here is lost and its retry is dropped as a repeat. This order keeps
     that window to the grace period; it does not close it.
  3. The bridge enters one drain state, checked everywhere work starts: the web
     app and `/v1`, MCP calls, the chat queue (which keeps waiting messages
     queued instead of marking them for review), approval continuations, 2.0
     channel routing (deliveries stay saved and are routed after the start, as
     today), the scheduler and the workflow queue. Today the bridge only counts
     running turns (`_InFlight` in `hubzoid/server.py`); it has no way to
     refuse new ones.
  4. Running turns get up to `--grace` seconds (default 120), with their leases
     and saves still running. Then the process stops and starts.
  - A reply still running at the deadline ends as **interrupted** ("The agent
    restarted during this reply"), keeps its partial text, and does not let
    the next queued message start. Today a graceful stop cancels replies, which
    records them as stopped (`hubzoid/chat/runs.py`, `hubzoid/chat/engine.py`),
    and finishing one can admit the next queued message.
  - Workflow runs (DBOS) and approvals waiting for a person resume after a
    restart **on the same code**. A run started by different workflow code or
    a different Hubzoid version is cancelled at start, by design
    (`_cancel_runs_from_other_code` in `hubzoid/workflows/runtime.py`). So an
    upgrade, unlike a restart, cancels runs that are waiting; `hubzoid status`
    lists them first, and the upgrade steps say to let them finish.
  - 1.x Slack (Socket Mode) has no connection while its hub restarts. Messages
    sent in those seconds may be missed; Slack's own redelivery is not
    something Hubzoid controls.
- Example: with 11 hubs, `hubzoid restart sales-hub billing-hub` restarts only those
  two, one after the other.

**A5. Reload: a checked restart of one hub.**

Today a hub's instructions and skills are built into its runtime when the
bridge starts (`hubzoid/factory.py`, `hubzoid/factory_claude.py`). Only a
published instruction change rebuilds the chat runtime in place
(`runtime_reloader` in `hubzoid/chat/authoring_routes.py`), and other surfaces
(MCP, 1.x channels, workflows) keep the runtime they started with. Building
in-place generations for every surface is a large, risky piece of work.
Instead:

| Change | Takes effect |
|---|---|
| Knowledge files, `hitl.yaml` | When next read, as today |
| Instructions published through hub changes | The chat runtime is rebuilt, as today; `hubzoid reload <hub>` brings the other surfaces along |
| Anything else in the hub folder: `AGENTS.md` edited by hand, skills, `channels/`, `connectors/`, cards, `.env`, `tools_local/`, `restricted/`, packages | `hubzoid reload <hub>` |
| `brand`, `chat`, `diagnostics` in `hubzoid.yaml` | Every bridge re-reads the file when it changes, as `hitl.yaml` is read today |
| The hub list in `hubzoid.yaml` | `hubzoid reload` starts new hubs and drains removed ones |
| `sign_in`, `public_url`, deployment `.env` | Restart the service |

- `hubzoid reload <hub>` validates the whole hub folder first. If anything is
  invalid it changes nothing and exits 1 naming the file and line. Otherwise it
  does the drained restart of A4. `hubzoid restart` skips the check, for when
  you need the process back regardless.
- A deployment file that becomes invalid while running changes nothing: each
  bridge keeps the version it has, and `status`, doctor and Settings say since
  when the file is invalid. At start, an invalid deployment file stops the
  start (it holds policy: terms acceptance, sharing, exports, file storage).
- `hubzoid status` compares what each hub started with (digests of its folder,
  `.env`, `tools_local/`, `restricted/` and requirements) with the folder, and
  says "reload needed: .env changed". Nothing is silently stale.
- **Edge routes change without restarting the edge.** The supervisor writes
  the route table to a file in the data folder (replaced atomically) and the
  edge re-reads it. A removed hub stays routed until its drain finishes, then
  answers 503. Today routes are fixed when the edge starts (`hubzoid/edge.py`).
- No file watcher in this release.

**A6. Channels by file.**

- A channel is on when its file in the hub's `channels/` folder says
  `enabled: true`. Its secrets come from `.env` (or the secret store, see B8).
  There are no flags.
- 2.0 connections (Slack, GitHub, Linear, email, WhatsApp Cloud) are files
  already. The file becomes the only copy: start and `hubzoid reload` read it,
  and `hubzoid channels apply` becomes "validate, then reload this hub".
  Pause and resume already work by rewriting `enabled:` in the file
  (`set_enabled` in `hubzoid/channels/config.py`); they keep doing that. A
  read-only hosted folder can't be paused from Settings; stop the hub or
  change the file in Git.
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
- Webhooks all arrive on the one public port, at the addresses they use today
  (`/webhooks/<hub>/…` for 1.x surfaces and workflow events,
  `/api/hubs/<key>/channels/<id>/webhook` for 2.0 connections). Providers have
  these registered. A new `/b/<hub>/webhooks/<id>` shape was considered and
  left out: it would add a third alias with its own rewrite and collision
  rules for no new capability.

**A7. Addresses.**

- Links (set-password, approvals, waiting-for-you, shares, downloads) are
  built from the deployment's public origin, never from a hub's path.
- Each hub has `/b/<slug>/…`: `api`, `v1` (personal API keys), `mcp`,
  `artifacts` and `branding`. The first hub also answers at `/`, so
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
- **The cutover is offline.** Today the reference deployment's bridges and
  inbound servers restart on their own, and each process upgrades the shared
  store when it first needs it (`hubzoid/migrations/__init__.py`). A new
  process must never drop tables while old ones still serve. So:
  1. Let waiting workflow runs finish (A4); `hubzoid status` on the old
     version isn't available, so the runbook gives the query.
  2. Stop and disable every old unit.
  3. `hubzoid backup`, and `hubzoid backup verify` it.
  4. Install the new version, then `hubzoid upgrade`: it writes `hubzoid.yaml`
     and the channel files from the old command lines and `.env` files,
     moving each value as it is (no secret is printed), exports every setting
     that exists only in the database (C2), and runs the migrations once.
  5. Install and start `hubzoid.service`.
  Going back means restoring the backup (database, data folder and deployment
  key together), the old version and the old units.
- A hub that ran alone and later joins a multi-hub deployment needs its
  accounts and chats merged into the deployment's store. That is a separate,
  explicit command (`hubzoid deployment add <hub>`), never automatic, and not
  part of this work.
- The reference deployment: one unit replaces the unit per hub, the inbound
  units and the gateway's. These lines change:

| Today | After |
|---|---|
| `hubzoid gateway <hubs> --no-bridges --name … --public-url …` (gateway unit), a unit per hub `hubzoid run <hub> --no-ui [--slack] --bridge-port 800X`, and `hubzoid inbound run <hub>` units | `hubzoid.yaml` with `hubs`, `name`, `public_url`; one `hubzoid.service` |
| A hub with `--slack` with `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` in its `.env` | `channels/slack-assistant.yaml`; the same two `.env` lines |
| Nurturing: `WHATSAPP_*` in its `.env`, served by an inbound unit | `channels/whatsapp.yaml` (`provider: whatsapp_inbound`) |
| Support: `WEBHOOK_INBOUND_SECRET`, `WEBHOOK_INBOUND_NAME` | `channels/<name>.yaml` (`provider: webhook`) |
| `HUBZOID_AUTH` in hub `.env` files | `sign_in:` in `hubzoid.yaml`, same value; then delete the lines |
| `ENABLE_SIGNUP` in hub `.env` files | Still a 2.0 setting (`hubzoid/auth/routes.py`): move it, same value, to the deployment `.env` |
| `WEBUI_NAME` in hub `.env` files | Still read as the name until one is set (`hubzoid/appmode.py`): set `brand.identity.name`, then delete |
| `DEFAULT_USER_ROLE`, `BYPASS_MODEL_ACCESS_CONTROL`, `WEBUI_SECRET_KEY`, `LICENSE_KEY` | Retired 1.x names (`appmode.RETIRED`): delete |
| `BRIDGE_PORT`, `BRIDGE_API_KEYS` in hub `.env` files | Optional; delete to let the supervisor assign them |
| `HUBZOID_ADMIN_EMAIL` in the gateway's systemd drop-in | `owner:` in `hubzoid.yaml` |
| `HOST` in two hub `.env` files | Checked during `hubzoid upgrade`; the bind address comes from `--host` |
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

**A broken file never loosens anything, and says so.**

- At start, an invalid `hubzoid.yaml` stops the start. Its `brand` and `chat`
  sections hold policy as well as looks: terms that must be accepted
  (`hubzoid/chat/routes.py` fails closed when it can't tell), and sharing,
  copying, exporting and file storage, where the product default is "on"
  (`hubzoid/chat/sharing.py`). A typo must not switch those.
- At start, an invalid file for one connector, channel or card leaves that one
  off; the hub runs.
- While running, an edit that makes a file invalid changes nothing (A5).
- Doctor, `hubzoid status` and Settings name the file, the line and the
  reason.

| Setting | Home | Settings shows | The database keeps |
|---|---|---|---|
| Brand | `brand:` in `hubzoid.yaml`, images and `theme.json` in `branding/` | Read-only, with the file to edit | Each person's acceptance of the terms and dismissed announcements |
| Chat defaults | `chat:` in `hubzoid.yaml` | Read-only | Each person's own appearance choices |
| Diagnostics limits | `diagnostics:` in `hubzoid.yaml` (open question 3) | Read-only | Diagnostic runs and support bundles |
| Agent instructions | `AGENTS.md`, `agents/`, `skills/`, `prompts/`; history in Git | Edits go through hub changes (B4) | Hub change proposals and decisions |
| Connectors | `connectors/<id>.yaml` in the deployment folder, and each hub's `connectors/.mcp.json` | Read-only, plus "Connect my account" | Each person's tokens, and clients registered with a provider |
| Who may start work from a channel | Database: a grant in Settings | Editable, like other grants | The grants |
| Channels | `channels/<id>.yaml` | Read-only, plus status, deliveries, threads and account links | Deliveries, threads, links, outbox, provider cursors |
| Cards | `cards/*.yaml`, `mcp-apps.yaml` | Read-only | Card instances, the card pages already shown, and the card configuration each instance was issued under |
| Hub setup values | `hub.settings.yaml` (already one file, written by the Setup tab) | Editable, writes the file | Nothing |
| Secrets a person types | `.env`, `restricted/.env`, the host, AWS (open question 1) | Names and status, never values | Nothing (open question 1) |
| Secrets Hubzoid obtains itself | The encrypted store | Names and status | The encrypted values |
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
- `hz_authoring_drafts` and `hz_authoring_versions` go. `hubzoid upgrade`
  turns each open draft into a pending hub change, and writes published
  versions that never reached Git to the data folder before the tables go.
  Personal prompts (`hz_personal_prompts`) stay.
- A hub that isn't in a Git repository has no history; doctor says so.

**B5. Connectors.**

- A connector added in Settings today has one identity for the whole
  deployment: tokens are keyed by person and connector id
  (`hz_connector_tokens`), and a dynamically registered client belongs to the
  connector. So its definition belongs to the deployment, not to each hub:
  `connectors/<id>.yaml` in the deployment folder (the hub folder for one hub)
  with the hubs that use it, its name, URL, how people sign in (`oauth` per
  person, `shared` company key, or `none`), scopes, the tool allow-list,
  whether writes need approval, and whether it is restricted.
- Its secrets (a pre-registered OAuth client secret, a shared company key) are
  named by reference (`client_secret: env:GITHUB_MCP_CLIENT_SECRET`), never
  written in the file.
- If a connector's URL or sign-in type changes, people's tokens for it are
  marked "connect again", so a token is never sent to a different server.
- A hub's `connectors/.mcp.json` stays as it is, for servers the runtime
  starts or calls with hub-wide credentials. It is Claude Code's own format,
  which doesn't describe per-person sign-in.
- The database keeps only what people and providers create: each person's
  tokens, Hubzoid's own MCP sign-ins (`hz_mcp_oauth`), sign-in flows in
  progress, and dynamically registered clients.
- Settings shows connectors read-only, with "Connect my account".
- Console connectors came in 1.2, so released deployments may have some.
  `hubzoid upgrade` writes each one to the folder and its secrets to the
  deployment `.env` before the cleanup. There is no release where both are
  read.

**B6. Channels.** The file is the only copy (A6).
`hz_channel_connections` keeps the status and provider state, and stops
storing the configuration. A hosted hub whose channel configuration exists only
in the database (`hubzoid/channels/config.py` keeps it there for read-only
folders) gets it written out by `hubzoid upgrade`, to commit. `hubzoid channels export` is
removed. With `HUBZOID_DATA_DIR`, a channel is set up by committing its file,
like eve's `agent/channels/*.ts`.

**B7. Cards.** `cards/*.yaml` and `mcp-apps.yaml` are read at start and
reload, with no apply step. `hz_app_config_revisions` stays, as a record: when
a hub loads a card configuration it hasn't seen, its digest and content are
recorded. A card instance may call only the tools allowed both when it was
issued and today (`granted` in `hubzoid/apps/cards.py`), so the record of what
it was issued under must stay, or old cards would break or gain tools. Card
instances and the pages already shown stay too.

**B8. Secrets (open question 1).** Two kinds of secret exist:

- Keys a person types (model keys, tool tokens).
- Keys Hubzoid obtains itself through a sign-in or setup flow: the GitHub App
  key from the manifest flow (`hubzoid/channels/github/setup.py`), Linear and
  mailbox OAuth tokens. These have no file to live in.

Choices:

- **(a) `.env` and the host's environment only.** Remove the store entirely.
  Generated keys go to files in the data folder. Simplest rule, the same as eve
  and Mastra; removes about 1,400 lines (`hubzoid/secretstore/`) and its uses
  in 12 modules, and changes the three setup flows above.
- **(b) Keep the store as it is.** `.env` wins, and a stored value it
  overrides is kept but unused (`hubzoid/secretstore/layer.py`); removing the
  `.env` line silently brings the old value back. Two homes for the same name:
  this breaks the rule.
- **(c) Split by kind (recommended).** Keys a person types live only in `.env`,
  `restricted/.env`, the host or AWS. Settings, the CLI and the API stop
  offering to store them; they show each name's status and where it is set.
  The store keeps only keys Hubzoid obtains itself, under names an operator
  source can't set. Each name has exactly one home, and the setup flows don't
  change. Typed values already stored are moved by `hubzoid upgrade` to the
  `.env` of their scope.

**B9. `identity/access.csv`.**

- Hubzoid stops reading it. It is used for three things today:
  - labels (groups) on people, which have granted nothing in the web app,
    MCP or tools since 1.1;
  - on WhatsApp and Telegram, a number-to-person map when the hub has no
    `identity/access.py`;
  - **a gate:** a 2.0 channel's `allowed_groups` lets only people with one of
    those labels start work from it, and an empty list lets nobody
    (`_groups_ok` in `hubzoid/channels/router.py`).
- Phone numbers are recorded on people in Settings, which inbound channels
  already check first.
- The gate moves to the database, where access lives: a grant per channel in
  Settings ("may start work from this channel"), the same shape as a
  connector's grant. It is checked together with access to the agent, as
  today. It is never widened silently: `hubzoid upgrade` turns each
  `allowed_groups` entry into grants for the people the CSV gives that label,
  and a channel file that still has `allowed_groups` with no grants made stays
  off, with a message saying why.
- `identity/access.py`, a hub's own code to look a number up in its systems,
  stays. The reference deployment's one roster reads its own CSV from inside
  `access.py` and defines no groups, so it doesn't change.
- Doctor names a hub that still has `identity/access.csv`.

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
That design stays. Each hub's DBOS file and `hub.db` stay too: the DBOS file
lets a hub recover its own workflows when it restarts alone, and `hub.db`
holds only 1.x WhatsApp and Telegram history behind a gateway. Moving that is
not needed for any goal here.

**C1. Tables that go in this work:** only those that B leaves unused.

| Tables | Why | First shipped |
|---|---|---|
| `hz_branding_drafts`, `_reviews`, `_revisions`, `_activations`, `_active`, `_assets` (6) | B1. `hz_branding_legal` and `_dismissals` stay: they are each person's | 2.0 (`op_0021`) |
| `hz_chat_config_heads`, `_revisions` (2) | B2. `hz_chat_preferences` stays | 2.0 (`op_0020`) |
| `hz_admin_config_plans`, `_heads`, `_revisions` (3) | B3, if file-only. Diagnostics tables stay | 2.0 (`op_0022`) |
| `hz_authoring_drafts`, `_versions` (2) | B4 | 2.0 (`op_0031`) |
| `hz_connector_agents` (1), and the definition columns of `hz_connectors` | B5. The table keeps only dynamically registered clients | 1.2 (`op_0012`, `op_0016`) |
| `hz_channel_connections` configuration columns | B6 | 2.0 (`op_0070`) |

`hz_app_config_revisions` stays (B7). `hz_chat_operations` stays: despite its
name it is the shared idempotency table for every control route. `hz_secrets`
stays under B8 (c).

**C2. Data is exported before anything is dropped.** `hubzoid upgrade` writes
every populated row that has a new home: the active brand and its images, the
active chat defaults and diagnostics limits, channel configuration that exists
only in the database, Console connectors, open Workspace drafts (as pending hub
changes) and published versions not in Git, and typed secrets (B8). It then
compares the folder with the database. The cleanup migration refuses to run
while any populated table has no matching export, and says which.

**C3. Later, as separate changes** (not needed for one run mode or one home):

- Parity tooling (`hz_parity_*`, `hubzoid/parity/`, the Settings tab):
  internal release tooling. A small follow-up change removes it from the
  package.
- `hz_shares`, pre-2.0 read-only links: the Open WebUI importer still writes
  them (`hubzoid/migrate_openwebui.py`) and a reader keeps them working. They
  go when the importer converts them to 2.0 links.
- Inbound history out of `hub.db` into the shared store.
- **Identities.** `hz_users` (the account), `hz_user_identities` (company
  sign-in bindings), `hz_directory_accounts` (directory bindings) and
  `hz_identities` (the access subject: email, `owui_id`, which is now the
  account id for 2.0 accounts, phone, pending) overlap. The target is one
  account table, with phone and pending invitations on it, that the bindings
  point at; `owui_id` becomes `account_id`. This touches sign-in and access in
  about a dozen modules, so it comes after the 2.0 launch with its own proof
  (open question 5).
- Kept on purpose: `hz_run_identities.legacy` marks runs moved from Open WebUI
  whose owner couldn't be proven, an audit fact. `hz_identity_attrs` holds
  per-hub attributes of a person that workflows read
  (`hubzoid/workflows/context.py`); only tests write it today, and it is
  decided with the identity work.

**C4. How the migrations run.**

- New tables and columns come in add-only migrations. One forward cleanup
  migration at the end drops what C1 lists, with `DROP … IF EXISTS`, so 2.0
  development databases and 1.3.2 databases (which never had most of them)
  reach the same head.
- Migrations `op_0020` to `op_0071` stay exactly as they are, because
  development databases have run them. No revision id is ever reused (F11).
- The supervisor runs migrations once before starting any child (A3), during
  the offline cutover (A9).
- Tests upgrade a 1.3.2 database and the 2.0 reference copy (stamped
  `op_0071`) to the new head on SQLite and PostgreSQL, and check that people,
  chats, access, approvals, runs and the exported settings survive. They also
  stop the cleanup half way and check that a second start finishes it.

## Scope and non-goals

- No change to a hub's required files. Every new file is optional.
- No hosting-provider work (Railway, Maritime), and no several-hub hosted
  containers (`HUBZOID_DATA_DIR` stays one hub).
- No new Settings screens. Brand, chat defaults, connectors, channels and cards
  become read-only views; instruction edits use the existing hub changes card;
  the channel grant uses the existing grants screen.
- No in-place runtime generations (A5) and no file watcher.
- No new webhook address shape (A6).
- Not merging the stores of hubs that were run apart (`hubzoid deployment
  add`, A9).
- Not moving 1.x Slack, WhatsApp, Telegram or webhook users to 2.0 channel
  connections (A6).
- Not the C3 items.
- No separate units per hub (open question 4).
- Model defaults don't change: `claude-local` uses Claude Code's default and
  `codex-local` uses Codex's.

## Open questions for the founder

1. **Secrets:** split by kind (B8 c, recommended: typed keys only in `.env`,
   the store only for keys Hubzoid obtains itself), `.env` only (B8 a), or keep
   the store as it is (B8 b)?
2. **The deployment file:** `hubzoid.yaml` in the deployment folder
   (recommended: short and easy to find, next to the hub folders it lists), or
   another name or place (`deployment.yaml`, inside `.hubzoid/`)?
3. **`admin.yaml`:** file-only as `diagnostics:` in the deployment file
   (recommended), or keep its plan and apply step?
4. **Separate units per hub:** leave them out (recommended: `hubzoid restart`
   covers the need and it is one code path), or ship `hubzoid@<hub>.service`
   and an edge unit as well?
5. **Identity merge (C3):** after launch as its own change (recommended), or
   in this work?

## Definition of done

- `hubzoid run ./hub` and `hubzoid run` with a two-hub `hubzoid.yaml` start
  the same processes in the same shape. `hubzoid gateway` is an alias with a
  notice.
- With 11 hubs running, `hubzoid restart` on 2 of them while chats stream on
  the other 9: the 9 streams finish, the 2 hubs drain and come back, and
  nothing else restarts.
- A killed bridge comes back on its own. Its routes answer 503 meanwhile,
  never another agent's answer, and the other agents keep answering.
- A broken `tools_local` file fails only its hub, which is marked failed with
  the reason, while the others serve.
- A restart during a waiting approval and during a running workflow: both
  resume. An upgrade lists waiting runs first.
- A reply cut by the grace deadline ends as interrupted with its partial text,
  and the next queued message waits.
- A 1.x WhatsApp or Telegram delivery acknowledged just before a restart is
  still answered.
- A channel webhook round trip on the shared port, turned on by a file alone.
- `hubzoid status`, `reload`, `logs`, `stop` and `start` work; status says
  when a reload is needed; `reload` refuses an invalid folder.
- Brand, chat defaults, connectors, channels and cards each come from one
  file, Settings shows them read-only, and the C1 tables are gone after the
  cleanup migration, with every populated row exported first.
- The five bugs in the Problem, and the route pinning in A3, have regression
  tests that run on the one code path.
- Upgrades from 1.3.2 and from the 2.0 reference copy keep people, chats,
  access, approvals, runs and settings.
- The DBOS ownership-loss acceptance test
  (`tests/test_postgres_acceptance.py::test_postgres_owner_loss_stops_claiming_and_keeps_work_recoverable`)
  passes unchanged. Skipped and timed-out checks are listed, not counted as
  passes.

## Proof

Automated, marked `slow` (they start subprocesses): supervisor restart with
backoff and the failed state; drain admission on each producer; the grace
deadline; supervisor death (no orphans); edge death and restart; two control
commands at once; repeated reloads; route pinning with the first hub down;
cleanup interrupted half way.

On the reference copy (all 11 hubs, the 8 GB machine), in the browser, with
screenshots, saved to `HubzoidAgent/factory/one-run-mode/proof.html`:

- the same checks in a one-hub and an 11-hub shape;
- restart 2 hubs while chats stream on the other 9;
- kill one bridge process and watch it come back alone;
- restart during a waiting approval and during a running workflow, and check
  both resume;
- break one hub's `tools_local` and check only that hub fails;
- a channel webhook round trip on the shared port, and a generic webhook
  acknowledged just before a restart;
- memory of every process at start and after an hour of use, compared with
  today's gateway, plus any SQLite lock errors and restart times.

Not provable on the copy, and said so on the page: real Slack, WhatsApp and
Telegram traffic (its secrets are blanked), and a live production upgrade.

## The Codex review and what changed

Codex (gpt-6.1-sol, high) reviewed revision 2 on 20 Oct and raised 24
findings. Each was checked against the code; all held. The main changes:

- Upgrade steps: `ENABLE_SIGNUP` and `WEBUI_NAME` are still read, so they are
  moved, not deleted; values move as they are (A9).
- A broken deployment file no longer falls back to defaults, which would have
  turned sharing on or dropped required terms (B).
- `allowed_groups` is a gate, not a label: it becomes a grant, never widened
  silently (B9).
- Every database-only setting is exported before the cleanup, which refuses
  to run otherwise (C2); the cutover is offline with migrations run once (A9,
  A3).
- State never moves; several-hub `HUBZOID_DATA_DIR` is dropped (A2).
- Inbound history, `hz_shares` and parity are deferred (C3); card revisions are
  kept as an authorization record (B7).
- The edge pins hub routes and fails over only deployment-wide ones (A3).
- Drain is one admission barrier across every producer, with an order for 1.x
  channels and an honest limit for 1.x deliveries (A4).
- Reload is a checked restart of one hub instead of in-place runtime
  generations, which the code doesn't provide today (A5).
- Restarts on the same code resume workflows; an upgrade cancels waiting runs
  by design, and the steps say so (A4).
- Connectors are deployment-owned, matching their database identity (B5).
- The secrets recommendation changes to a split by kind that keeps one home
  per name (B8).
- A precedence table, a control-socket contract, and a larger proof.
