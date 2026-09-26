# Changelog

All notable changes to Hubzoid. Versions follow the package version in
`pyproject.toml`; each release tag `vX.Y.Z` must have a section here.

## Unreleased

### Workflows
- Scheduled workflows and markdown tasks run as an ordinary account: `run_as`
  (decorator or frontmatter), else `HUBZOID_WORKFLOW_USER` (hub, then deployment),
  else the owner recorded at setup (locally `admin@localhost`). The account is
  captured once per run, rechecked before every protected call, and never
  swapped for another. `hubzoid schedule list` shows it.
- `hub.state` is per account (plus `hub.shared_state`), `hub.run_dir` is a
  private per-run folder, and `hub.connection(app, ref=None)` returns the run
  account's own credential.
- `hub.publish_artifact(...)` publishes a generated file as a private report
  with a viewer at `/portal/artifacts/<id>`: share with people, groups or the
  hub, or (with the new `share_public_links` permission) by an expiring public
  link. HTML reports run sandboxed with no network access.
- `hub.send_email(...)` emails the run's own account over SMTP (`HUBZOID_SMTP_*`)
  or to a preview outbox, with delivery records that never resend an ambiguous
  send. Markdown tasks opt in with `publish_artifacts: true` / `send_email: true`.

## [1.0.1] - unreleased

Upgrading from 0.9.x: read [docs/UPGRADING.md](docs/UPGRADING.md) first.

### Accounts, connections and secrets
Everything here is off or inert until enabled. Hubs on legacy Open WebUI group
access behave as before.
- Admin Console account management: People, Add account creates a chat account
  through Open WebUI's account API (role user). The password is shown once to
  share manually and is never stored or logged. Org admins can reset passwords,
  approve, change the chat role and delete accounts. Google sign-in onto a
  pre-added account needs `OAUTH_MERGE_ACCOUNTS_BY_EMAIL=true`.
- One authorization service decides every Console and API change. On managed
  hubs a delegate can grant or remove only what they hold, minus Manage access,
  and cannot change their own access, an org admin's, public access, or other
  accounts. `/portal/api` also accepts an Open WebUI API key (`Bearer sk-...`).
  Refusals carry a `code`.
- Optional management tools (`HUBZOID_MANAGEMENT_TOOLS`) let an agent propose
  people and access changes. The change applies only after the same manager
  confirms the exact plan in the Console. Proposals are single use, expire
  (`HUBZOID_CHANGE_REQUEST_TTL`) and are audited with their surface.
- `HUBZOID_HIDE_OWUI_USERS=true` sends Open WebUI's Users page to Console People
  and refuses its account-admin writes.
- Connection journeys (`HUBZOID_CONNECT_JOURNEY`): "connect my Gmail" from chat
  or WhatsApp sends a personal link bound to that person. The result is checked
  with the provider, confirmed on a browser page and back in WhatsApp, with an
  optional one-time continuation of the waiting request. Built on Open WebUI
  native MCP, with Composio links routed through the same journey.
- Personal Open WebUI MCP connections now work on the OpenAI Agents SDK and
  Codex backends as well as Claude. On managed hubs each app needs its
  `connector_<app>` capability. `connector_` is a reserved capability prefix.
- Agents, Access, Edit access groups capabilities as Hub access, Hubzoid tools,
  Custom restricted tools, Workflows and Administration, and hides empty groups.
  Built-in capabilities register their label, group, surfaces and required
  settings (`hubzoid/capabilities.py`), so a new one appears without its own
  screen. Configuration is shown separately from permission: a missing setting
  shows a short status such as "Jev key missing" and never grants or blocks
  anything by itself. Grants for capabilities that no longer exist stay visible
  and removable. `identity/permissions.yaml` can relabel custom restricted tools
  only, not built-ins.
- Public access is removed. New "Everyone signed in" grants are refused in the
  Console, the API, agent-proposed changes, `hubzoid grant '*'` and the store.
  Existing ones keep working and appear as an "Everyone signed in" row that an
  org admin can remove after granting named people. Migrating a legacy hub that
  was open to everyone carries that over and says so in the plan.
- Optional AWS Secrets Manager secrets per layer (`AWS_SECRET_NAME` with
  `AWS_REGION` for the deployment, `HUBZOID_HUB_SECRET_NAME`,
  `HUBZOID_RESTRICTED_SECRET_NAME`), read at start with boto3's normal
  credential chain. Within a layer the secret wins over the file. Rotation
  takes effect on restart. `hubzoid doctor` reports each key's layer (names
  only) and whether each secret is reachable (`--skip-secret-fetch`).
- Agent child processes (the `claude` CLI and its MCP servers) no longer
  inherit restricted-tool values, Hubzoid service secrets, SMTP credentials or
  AWS credentials. Open WebUI no longer receives `HUBZOID_*` settings.

### Runtime and licensing
- Local Codex backend through the pinned 0.147.0 app-server protocol, using the
  shared guarded tools and isolated per-request threads. Fresh interactive setup
  selects an authenticated local CLI, asking once when both are usable.
- Hubzoid-owned code is licensed under Apache-2.0. Distributions include LICENSE
  and NOTICE; dependency and bundled font licenses remain intact.

### Console
- Named Admin Console, with a sidebar entry above the chat profile and an icon
  when collapsed. Only authorized administrators see the entry.
- Opens on **Agents**, with five summary cards (messages/conversations, users,
  tokens, workflow runs and approximate cost) above agent cards. Update time
  sits beside Refresh; global Runs navigation and account-management shortcuts
  are removed. Agent-level runs and existing direct links remain available.
- Agent cards show tokens and approximate cost for the selected dashboard period.
- Capability explanations move into accessible help; compact restriction labels
  stay visible. Login accounts are still created in Open WebUI.
- Dark theme uses Studio charcoal, clear orange actions and deep semantic tag
  backgrounds with readable foregrounds.
- Public email and SSO registration default to off. Administrators create
  accounts in the chat app's admin panel; explicit operator overrides remain.
- Every chat turn and workflow model call writes a usage row (`hz_usage`): time,
  hub, surface, user, chat, model, tokens, estimated cost, status and duration.
  No message content.

### Security
- The edge no longer keeps cookies across visitors. Its shared upstream client
  stored Open WebUI's sign-in cookie and sent it with later requests that had no
  cookie of their own, so an anonymous visitor could receive the last signed-in
  user's session. Earlier releases with the edge are affected. After upgrading,
  rotate `WEBUI_SECRET_KEY` to end any session that may have leaked.
- A user's personal Open WebUI MCP connection is used only on surfaces allowed
  to reach restricted tools (`HUBZOID_RESTRICTED_SURFACES`). A shared Slack
  channel mention no longer carries the mentioner's token.
- Open WebUI API-key, group and connected MCP OAuth lookups honor PostgreSQL
  `DATABASE_URL` and `DATABASE_SCHEMA`, including shared gateways. Database
  failures deny access without falling back to stale SQLite credentials.
- Chat and MCP file tools refuse dotenv files, databases and sidecars, private
  runtime state and symlinks into those locations. Both search backends check
  every file before reading; SQLite signatures catch renamed database files.
- Knowledge/skill/agent loaders cannot publish protected files through aliases.
  Current-chat uploads/artifacts remain scoped and subject to secret-file checks.
- The Jinja renderer now uses the advertised sandbox, blocking Python object
  traversal that could bypass file-tool restrictions.

### First use and usability
- Console exposes the built-in `curator` capability as Save shared knowledge,
  including for Markdown workflow service identities. The compact wordmark is
  readable at its intended size and dark warning helper text has checked contrast.
- The verified configured owner receives Console and hub entry access once.
  Fresh hubs start with managed access; existing hubs retain their migration
  mode. Later sign-ins do not restore revoked grants.
- Chat's agent picker respects Hubzoid entry permissions, including for chat
  administrators. Startup refreshes owned bridge connections without clearing
  unrelated saved configuration. The upstream first-run changelog is hidden.
- Console follows the current Studio theme with local fonts, compact alerts,
  accessible headings and controls, account guidance, readable schedules,
  inspectable results, and a combined Agents dashboard. Authentication, permission and
  service failures offer distinct next steps.
- Chat conversation/message counts exclude background title and suggestion
  calls, while their tokens and estimated cost remain included. Tool-start
  markers no longer imply success before a result arrives.
- The first workflow scaffold runs manually without a model or external
  service, and prints the correct command. README and setup, access, workflow,
  upgrade and design guides reflect the current product direction and behavior.

### Workflows and schedules
- Markdown schedule tasks (`schedule/*.md`) run on the hub's DBOS engine, with
  the same files, timing and catch-up rules. Each run is split into work,
  commit, push and finish steps; a push is retried, interrupted work is
  reported rather than repeated, and every run appears in the Console. DBOS
  replaces the per-hub run lock and in-process execution, running one markdown
  task at a time per hub across processes. The 30-second tick now only decides
  what is due and queues each slot once, as run `md:<task>:<slot>`. The round
  harness is unchanged and runs inside the work step.
- `hub.call_llm` is one model call with no tools: text, JSON, or a validated
  Pydantic object (`response_model`), on LiteLLM models, `claude-local` and `codex-local`.
  `hub.call_agent` keeps the full agent. `hub.decide` (experimental) asks
  TypeSafe's Jev through OpenRouter for a typed decision.
- Code workflows run one at a time per workflow, side by side across
  workflows. Optional hub-wide cap: `max_concurrent_workflows`.
- `hubzoid schedule pause | resume | cancel`, recorded in the access log. The
  Console shows paused work but has no run buttons.
- Webhooks: GitHub's `X-Hub-Signature-256` signatures are accepted, and
  repeated deliveries (by delivery id, or an identical body shortly after) are
  dropped. A delivery counts as seen only once it is stored, so a crash never
  swallows the provider's retry. A webhook task is told the event files it owns
  (a line in its prompt, or `HUBZOID_WEBHOOK_EVENTS` for `run:` scripts), and a
  run that did not finish is retried once it has ended.
- A scheduled task whose run changed nothing no longer pushes.
- New sample: `hubzoid init <name> --template watchtower`.

### Access
- Tool decisions are stored in the database (`hz_access_decisions`) instead of
  monthly JSONL files, which are imported once. A restricted call whose
  decision cannot be recorded is refused.
- Scheduled markdown runs act as `workflow:md:<task>`, grantable like a person.
- A per-agent **Manage access** grant also lets that person open and chat with
  that agent, because every direct agent capability includes **Use this
  agent**. Restricted tools still need their own grant. Organization-wide
  administrator rights alone do not grant chat.
- The public port refuses `.` and `..` path segments (they could reach bridge
  paths outside the forwarded routes) and drops client-sent `X-Hubzoid-*` and
  `X-OpenWebUI-*` headers.

### Operations
- Gateway: any bridge can serve the Console and the agent picker's access
  check. When one bridge is down or restarting, the edge asks the next one, so
  restarting a hub no longer empties the picker for everyone.
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
- `hubzoid backup` and `hubzoid restore`: one archive of a deployment's
  databases, chat data and hub state, taken while chat keeps working. New
  scheduled runs are held and running ones finish first. Restore can move a
  deployment to new paths. PostgreSQL goes through `pg_dump`.
- `hubzoid doctor --json`: checks with stable ids (`auth.bridge_keys`,
  `db.operational`, `backup.age`, `scheduler.health`, ...) for scripts and
  monitoring. Doctor reads only.
- Hubzoid's own tables are versioned with Alembic and upgraded at start, with a
  lock for bridges starting together. A database from a newer release is
  refused.
- Pull requests run a light check (tests, Console lint and build). A release is
  built and tested from its tag before PyPI, the GitHub release and a
  multi-architecture image on GHCR are published.
- Backups leave database passwords out of the saved deployment manifest
  unless `--include-secrets`, and restore checks every target path in an
  archive before it touches anything.
- `hubzoid.__version__` comes from the package metadata.
- The workflow engine refuses to start, with a clear message, on Python 3.12
  with SQLite older than 3.42 (DBOS needs `unixepoch('subsec')`); doctor reports
  it as `deps.sqlite`. The Docker image moves to Debian 13 (SQLite 3.46) for
  this reason. Compose files for SQLite and PostgreSQL are in `docker/`.
- New docs: [workflows](docs/workflows.md), [backup](docs/BACKUP.md),
  [upgrading](docs/UPGRADING.md), SQLite or PostgreSQL and supported
  topologies in [deploying](docs/DEPLOYING.md), and [SECURITY.md](SECURITY.md).

### Security and stability (R0)
- OpenAI Agents SDK trace export is off unless `HUBZOID_OPENAI_TRACING=true`.
- Artifact download links are signed with a per-hub secret; links issued by
  earlier versions stop working. The public default bridge key `dev` is refused
  on `/artifacts`. Optional expiry: `HUBZOID_ARTIFACT_LINK_TTL`.
- Open WebUI branding is kept unless the hub has files in `branding/`. Admins
  can no longer open or export other users' chats (`ENABLE_ADMIN_CHAT_ACCESS`,
  `ENABLE_ADMIN_EXPORT` to allow).
- Workflow agent calls are not retried unless `agent_max_attempts` is set; a
  failed agent run fails the workflow run.
- DBOS 3.1. Runs are tied to the workflow code version; runs from other code
  are cancelled at start instead of blocking the queue. A markdown run that had
  not started yet is queued again under the new code first.
- Dependencies bounded and locked (`requirements.lock`); Docker image built from
  source with CPU-only PyTorch, publishing only port 3080.
