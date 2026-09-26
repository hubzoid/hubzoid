# Changelog

All notable changes to Hubzoid. Versions follow the package version in
`pyproject.toml`; each release tag `vX.Y.Z` must have a section here.

## [1.0.1]

Upgrading from 0.9.x: read [docs/UPGRADING.md](docs/UPGRADING.md) first. The
[release notes](docs/release-notes/1.0.1.md) summarize this release, its
upgrade requirements and its known limits.

### Workflows
- New code workflows: Python functions with `@workflow` and `@step` in
  `workflows/<name>/*.py`, run on a schedule or by hand on the hub's DBOS
  engine. A finished step is saved and is not run again when a run resumes. A
  step that was running when the process stopped runs again, so steps that
  change outside systems must be safe to repeat. `hubzoid new workflow`
  scaffolds a manual example that needs no model or external service.
- Markdown schedule tasks (`schedule/*.md`) run on the same engine, with the
  same files, timing and catch-up rules. Each run is split into work, commit,
  push and finish steps. A push is retried. Agent work interrupted by a
  restart is reported as interrupted and not re-run, and the next slot runs the
  task again. DBOS replaces the per-hub run lock and in-process execution,
  running one markdown task at a time per hub across processes. The 30-second
  tick only decides what is due and queues each slot once, as run
  `md:<task>:<slot>`. The round harness is unchanged and runs inside the work
  step.
- On Python 3.12 the workflow engine needs SQLite 3.42 or newer (DBOS uses
  `unixepoch('subsec')`), or PostgreSQL. Otherwise it refuses to start with a
  clear message, and `hubzoid doctor` reports it as `deps.sqlite`. Python 3.11
  is not affected. Check with
  `python -c "import sqlite3; print(sqlite3.sqlite_version)"`.
- `hub.call_llm` is one model call with no tools: text, JSON, or a validated
  Pydantic object (`response_model`), on LiteLLM models, `claude-local` and
  `codex-local`. `hub.call_agent` runs the full agent. It is not retried unless
  `agent_max_attempts` is set, and a failed agent run fails the workflow run.
- `hub.call_jev` (experimental) asks TypeSafe's Jev through OpenRouter for
  typed `noul`, `choice` and `score` decisions, one type or mixed in one
  request. It uses a dedicated `JEV_OPENROUTER_API_KEY` with no fallback to
  `OPENROUTER_API_KEY`, checks every answer against its question, and fails the
  step on an empty or malformed reply. Rate limits, server errors and timeouts
  are retried once. A call still in flight when the process stopped is made
  again on resume (at least once, not exactly once). The same adapter is a
  `call_jev` chat tool behind the **Call Jev** (`jev`) capability, granted to
  nobody by default.
- Code workflows run one at a time per workflow, side by side across
  workflows. Optional hub-wide cap: `max_concurrent_workflows`.
- DBOS 3.1. Runs are tied to the workflow code version. Runs from other code
  are cancelled at start instead of blocking the queue. A markdown run that had
  not started yet is queued again under the new code first.
- `hubzoid schedule pause | resume | cancel`, recorded in the access log. The
  Console shows runs and paused work but has no run buttons.
- Webhooks: GitHub's `X-Hub-Signature-256` signatures are accepted, and
  repeated deliveries (by delivery id, or an identical body shortly after) are
  dropped. A delivery counts as seen only once it is stored, so a crash does
  not swallow the provider's retry. A webhook task is told the event files it
  owns (a line in its prompt, or `HUBZOID_WEBHOOK_EVENTS` for `run:` scripts),
  and a run that did not finish is retried once it has ended.
- A scheduled task whose run changed nothing no longer pushes.
- New sample: `hubzoid init <name> --template watchtower`.

### Who a workflow runs as
- Scheduled workflows and markdown tasks run as an ordinary account: `run_as`
  (decorator or frontmatter), else `HUBZOID_WORKFLOW_USER` (hub, then
  deployment), else, on a Console-managed hub, the owner recorded at setup
  (locally `admin@localhost`). The account is captured once per run, rechecked
  before every protected call, and never swapped for another.
  `hubzoid schedule list` and the Console's **Runs as** column show it.
- `hub.state` is per account (plus `hub.shared_state`) and `hub.run_dir` is a
  private per-run folder. `hub.call_agent` acts as the run's account, so it uses
  that person's Open WebUI connections, never another's.
- A hub secret's `HUBZOID_WORKFLOW_USER` wins over the hub `.env`. Runs,
  `schedule list` and **Runs as** share one resolution.
- A hub's managers see each run's workflow, status, timing and a failure
  summary. The result and step outputs are shown only to the account the run
  acted as.
- A legacy hub (access still in the chat app) with no account configured keeps
  the service identity `workflow:md:<task>`, grantable like a person. New
  `workflow:*` service identities cannot be added in the Console. Existing ones
  stay, labelled "Legacy service identity", and runs that act as an account do
  not use their grants.

### Artifacts and email
- `hub.publish_artifact(...)` publishes a generated file (HTML, PDF, CSV,
  images and other formats) as a private artifact with a viewer at
  `/portal/artifacts/<id>`. Share it with people, groups or the hub, or by an
  expiring public link. HTML artifacts run sandboxed with no network access.
- **Share artifacts publicly** (`share_public_links`) lets its holder create
  links that anyone can open without signing in. Publishing never grants it.
  Removing it (or blocking the owner) ends that owner's public links for good,
  and granting it again does not revive them (`op_0007`). "Anyone with the
  link" and link creation are one step. A dead link shows a clear page. PDFs
  keep their file name.
- `hub.send_email(...)` emails the run's own account over SMTP
  (`HUBZOID_SMTP_*`) or writes it to a preview outbox. `accepted` means the
  SMTP server accepted the message, not that it reached an inbox. A resumed
  run does not send an accepted message again, and an ambiguous send is
  reported and never resent automatically. Markdown tasks opt in with
  `publish_artifacts: true` / `send_email: true`.
- Email preview files are created 0600 in 0700 folders.
- Download links for files an agent makes in chat are signed with a per-hub
  secret. Links issued by earlier versions stop working. The public default
  bridge key `dev` is refused on `/artifacts`. Optional expiry:
  `HUBZOID_ARTIFACT_LINK_TTL`.

### Admin Console and delegation
- The Admin Console at `/portal/` manages people, agent access and capabilities,
  and shows usage and runs. Authorized administrators see an **Admin Console**
  entry above their profile in the chat sidebar, with a shield-and-cog icon
  that keeps its name when the sidebar is collapsed.
- **Add user**, on an agent's Access page and on People, either picks an
  existing account or creates one (name, email and a typed or generated
  password to share manually) with its first access in one flow. The password
  is shown once and is never stored or logged. When Google sign-in and
  `OAUTH_MERGE_ACCOUNTS_BY_EMAIL=true` are configured, "Google sign-in only"
  creates the account with no password to share. A duplicate email offers
  "Grant access instead". A partial result keeps the account and offers "Try
  again" without creating a second one. Email-only pre-approval remains,
  clearly labelled. Public sign-up stays closed.
- Organization administrators approve pending sign-ups, reset passwords,
  change the chat-app role and delete accounts from a person's Details.
- Public email and SSO sign-up default to off (`ENABLE_SIGNUP`,
  `ENABLE_OAUTH_SIGNUP`). Explicit operator overrides remain.
- One authorization service decides every Console and API change. A delegate
  (Manage access on specific agents) grants or removes only what they hold in
  that agent, never Manage access itself. They cannot change their own access
  or an org admin's, and cannot approve, reset, block or delete accounts. They
  can create a normal account with access in the agents they manage.
  `/portal/api` also accepts an Open WebUI API key (`Bearer sk-...`). Refusals
  carry a `code`.
- A per-agent **Manage access** grant also lets that person open and chat with
  that agent, because every direct agent capability includes **Use this
  agent**. Restricted tools still need their own grant. Organization-wide
  administrator rights alone do not grant chat.
- Access goes to named people. Nobody can create new "Everyone signed in"
  access: the Console, the API, agent-proposed changes, `hubzoid grant '*'`
  and the grant store refuse it. Migrating a legacy hub that was open to all
  signed-in users carries that over as an "Everyone signed in" row, which keeps
  working until an org admin removes it.
- Edit access groups capabilities as Hub access, Hubzoid tools, Custom
  restricted tools, Workflows and Administration, in collapsible sections
  ("Restricted tools · 2 selected") with keyboard, touch and screen-reader
  controls. Empty groups are hidden. The review lists who, the agent, and what
  is added and removed. Success reads "Access updated".
- Built-in capabilities register their label, group, surfaces and required
  settings (`hubzoid/capabilities.py`), so a new one appears without its own
  screen. Configuration is shown apart from permission: a missing setting
  shows a short status such as "Jev key missing" and never grants or blocks
  anything by itself. Grants for capabilities that no longer exist stay visible
  and removable. `identity/permissions.yaml` can relabel custom restricted
  tools only, not built-ins. The built-in `curator` capability reads **Save
  shared knowledge**.
- Tool refusals, the management tools and the connection journey name
  capabilities by their Console label, with the id where a tool needs it.
  Management tools are hidden from people who manage nothing on every runtime.
  A held but non-delegable capability reads "Admins only".
- Optional management tools (`HUBZOID_MANAGEMENT_TOOLS`) let an agent propose
  people and access changes. The change applies only after the same manager
  confirms the exact plan in the Console. Proposals are single use, expire
  (`HUBZOID_CHANGE_REQUEST_TTL`) and are audited with their surface.
- The verified configured owner receives Console and hub entry access once.
  Fresh hubs start with managed access. Existing hubs keep their access mode
  until migrated. Later sign-ins do not restore revoked grants.
- Chat's agent picker respects Hubzoid entry permissions, including for chat
  administrators. A new account's agents are mirrored to the chat app at once,
  so the first sign-in shows them without a reload. A blocked person, or one
  with no agent yet, gets a notice in the chat instead of an empty picker.
- Tool decisions are stored in the database (`hz_access_decisions`) instead of
  monthly JSONL files, which are imported once. A restricted call whose
  decision cannot be recorded is refused.
- A gateway set up fresh with Console accounts hides Open WebUI's user list
  and refuses its account-admin writes (recorded in `deployment.json`,
  `HUBZOID_HIDE_OWUI_USERS` overrides, existing deployments unchanged). Open
  WebUI's Admin Panel entry then opens Settings > Integrations over Groups, and
  its Users tab opens Groups. Only Open WebUI administrators see the Admin
  Panel.
- The Console's service account reuses its Open WebUI token instead of signing
  in for every sync and account action, which could exhaust Open WebUI's
  sign-in limit for the owner's email.

### Personal MCP connections
- Each person's own Open WebUI native MCP connections work on all three
  runtimes. On Console-managed hubs each app needs its `connector_<app>`
  capability. `connector_` is a reserved capability prefix.
- Connection journeys (`HUBZOID_CONNECT_JOURNEY`, off by default): "connect my
  Gmail" from chat or WhatsApp sends a personal link bound to that person. The
  result is checked with the provider, confirmed on a browser page and back in
  WhatsApp, with an optional one-time continuation of the waiting request.
  Built on Open WebUI native MCP only. The optional Composio integration is
  unchanged and not part of it.
- Connection links work in real browsers (the page no longer strips its own
  origin), and a signed-out person comes back to the link after signing in.

### Runtimes
- Three runtimes: the OpenAI Agents SDK (LiteLLM models), the Claude Agent SDK
  (`claude-local`) and local Codex (`codex-local`). Codex runs through the
  pinned 0.147.0 app-server protocol, using the shared guarded tools and
  isolated per-request threads. Fresh interactive setup selects an
  authenticated local CLI, asking once when both are usable.
- On `claude-local` and `codex-local`, the agent is no longer shown controlled
  tools (`restricted/` modules, `remember`, `call_jev`) that the person may not
  use, as was already the case on LiteLLM models. Calls were already refused.
- `claude-local` no longer shows chat users the connectors of the Claude
  account the box is signed in to (claude.ai Gmail, Drive, Slack, ...) or MCP
  servers from that account's settings. Every Claude run (chat, `call_llm`, the
  eval judge) uses only the servers Hubzoid passes. The eval judge also no
  longer gets Claude Code's built-in tools.
- OpenAI Agents SDK trace export is off unless `HUBZOID_OPENAI_TRACING=true`.
- Usage rows name the model that answered, not the Claude CLI's background
  Haiku call.
- The bundled demo hub describes the three runtimes and current positioning.

### Console and chat
- The Console opens on **Agents**, with five summary cards (messages and
  conversations, users, tokens, workflow runs and approximate cost) above the
  agent cards. Agent cards show tokens and approximate cost for the selected
  period. Runs and schedules live in each agent. Existing direct links to the
  old global Runs page still work.
- The **Users** card counts the sign-in accounts in the viewer's scope (the
  deployment for an organization admin, accounts with access to their agents
  for a delegate), across however many hubs, whatever the period. Blocked
  accounts count. Service identities and email-only grants do not. An
  unreadable account directory shows as unavailable, never 0.
  `/portal/api/summary` adds `user_accounts`. `totals.active_users` is
  unchanged.
- Every chat turn and workflow model call writes a usage row (`hz_usage`):
  time, hub, surface, user, chat, model, tokens, estimated cost, status and
  duration. No message content.
- Chat conversation and message counts exclude background title and suggestion
  calls. Their tokens and estimated cost are still included. Tool-start
  markers no longer imply success before a result arrives.
- Chat shows a "Working on it…" status from send until the first words, on
  every runtime. A turn stopped before the first word, or cut off because the
  hub's bridge stopped, no longer keeps that line, also after a reload.
- The Console follows the Studio theme with local fonts, compact alerts,
  accessible headings and controls, readable schedules and inspectable results.
  Dark theme uses Studio charcoal, clear orange actions and readable tag
  colours. Authentication, permission and service failures offer distinct next
  steps.
- Agents show a spark icon, and role badges are muted and readable in both
  themes. Capability explanations move into accessible help, and compact
  restriction labels stay visible.
- Open WebUI branding is kept unless the hub has files in `branding/`. The
  upstream first-run changelog is hidden. Startup refreshes owned bridge
  connections without clearing unrelated saved configuration.
- Admins can no longer open or export other users' chats
  (`ENABLE_ADMIN_CHAT_ACCESS`, `ENABLE_ADMIN_EXPORT` to allow).

### Operations
- `hubzoid backup` and `hubzoid restore`: one archive of a deployment's
  databases, chat data and hub state, taken while chat keeps working. New
  scheduled runs are held and running ones finish first. Restore can move a
  deployment to new paths. PostgreSQL goes through `pg_dump`. Backups leave
  database passwords out of the saved deployment manifest unless
  `--include-secrets`, and restore checks every target path in an archive
  before it touches anything.
- `hubzoid doctor --json`: checks with stable ids (`auth.bridge_keys`,
  `db.operational`, `backup.age`, `scheduler.health`, `deps.sqlite`, ...) for
  scripts and monitoring. Doctor reads only.
- Hubzoid's own tables are versioned with Alembic and upgraded at start, with a
  lock for bridges starting together. Migrations are forward only. A database
  from a newer release is refused.
- Gateway: any bridge can serve the Console and the agent picker's access
  check. When a bridge refuses connections, the edge tries the next one, so
  restarting one hub no longer empties the picker. This covers the Console and
  the picker check only. It is not high availability for the gateway or the
  chat app.
- Gateway: a hub's `.env` stays with that hub. Sign-in and chat-app settings
  (`WEBUI_*`, `DEFAULT_USER_ROLE`, `ENABLE_SIGNUP`, OAuth, `HUBZOID_PUBLIC_URL`)
  are still taken from hub `.env` files when the gateway's own environment lacks
  them, and listed at start. `WEBUI_NAME` in a hub `.env` no longer overrides
  `--name`.
- SQLite databases wait up to 30 seconds for another writer's lock, since a
  gateway's bridges share one file.
- Standalone and gateway supervisors wait for child services to shut down before
  exiting. This lets SQLite close cleanly during container stop and avoids
  interrupting database cleanup before a rollback.
- Optional AWS Secrets Manager secrets per layer (`AWS_SECRET_NAME` with
  `AWS_REGION` for the deployment, `HUBZOID_HUB_SECRET_NAME`,
  `HUBZOID_RESTRICTED_SECRET_NAME`), read at start with boto3's normal
  credential chain. Within a layer the secret wins over the file. Rotation
  takes effect on restart. `hubzoid doctor` reports each key's layer (names
  only) and whether each secret is reachable (`--skip-secret-fetch`).
- Docker: the image is built from the root `Dockerfile` on Debian 13 (SQLite
  3.46), with CPU-only PyTorch, as an unprivileged user, publishing only port
  3080. Each release is also published to GHCR for amd64 and arm64. Compose
  files for SQLite and PostgreSQL are in `docker/`.
- Dependencies are bounded and locked (`requirements.lock`).
- Pull requests run a light check (tests, Console lint and build). A release is
  built and tested from its tag before PyPI, the GitHub release and the
  container image are published.
- `hubzoid.__version__` comes from the package metadata.
- New docs: [workflows](docs/workflows.md), [backup](docs/BACKUP.md),
  [upgrading](docs/UPGRADING.md), SQLite or PostgreSQL and supported
  topologies in [deploying](docs/DEPLOYING.md), and [SECURITY.md](SECURITY.md).

### Security
- The edge no longer keeps cookies across visitors. Its shared upstream client
  stored Open WebUI's sign-in cookie and sent it with later requests that had no
  cookie of their own, so an anonymous visitor could receive the last signed-in
  user's session. Earlier releases with the edge are affected. After upgrading,
  rotate `WEBUI_SECRET_KEY` to end any session that may have leaked.
- A user's personal Open WebUI MCP connection is used only on surfaces allowed
  to reach restricted tools (`HUBZOID_RESTRICTED_SURFACES`). A shared Slack
  channel mention no longer carries the mentioner's token.
- MCP credentials (each person's connector token, a hub server's headers and
  `env`) no longer appear in the `claude` process's command line, where other
  accounts on the machine could read them. They go in a per-turn file readable
  only by Hubzoid's account, removed when the turn ends. See docs/mcp.md for
  what this does not cover.
- Agent child processes (the `claude` CLI and its MCP servers) no longer
  inherit restricted-tool values, Hubzoid service secrets, SMTP credentials or
  AWS credentials. Open WebUI no longer receives `HUBZOID_*` settings.
- The public port refuses `.` and `..` path segments (they could reach bridge
  paths outside the forwarded routes) and drops client-sent `X-Hubzoid-*` and
  `X-OpenWebUI-*` headers.
- Open WebUI API-key, group and connected MCP OAuth lookups honor PostgreSQL
  `DATABASE_URL` and `DATABASE_SCHEMA`, including shared gateways. Database
  failures deny access without falling back to stale SQLite credentials.
- Chat and MCP file tools refuse dotenv files, databases and sidecars, private
  runtime state and symlinks into those locations. Both search backends check
  every file before reading. SQLite signatures catch renamed database files.
- Knowledge, skill and agent loaders cannot publish protected files through
  aliases. Current-chat uploads and artifacts remain scoped and subject to
  secret-file checks.
- The Jinja renderer now uses the advertised sandbox, blocking Python object
  traversal that could bypass file-tool restrictions.

### Licence
- Hubzoid-owned code is licensed under Apache-2.0 (0.9.x was MIT).
  Distributions include LICENSE and NOTICE. Dependency and bundled font
  licences remain intact.
