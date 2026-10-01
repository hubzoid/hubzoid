# Changelog

All notable changes to Hubzoid. Versions follow the package version in
`pyproject.toml`; each release tag `vX.Y.Z` must have a section here.

## [1.1.0]

Hubzoid 1.1 replaces Open WebUI with the Hubzoid web app. Upgrading from
1.0.x: read [docs/UPGRADING.md](docs/UPGRADING.md) first. The
[release notes](docs/release-notes/1.1.0.md) summarize the release, its
upgrade requirements and its known limits.

### Web app
- `hubzoid run` and `hubzoid gateway` serve the Hubzoid web app on the public
  port: chat at `/`, the Admin Console at `/portal/`, sign-in at `/auth`, one
  bundle and one sign-in for both. No Open WebUI process runs.
- Conversations with history, search, rename, archive and delete. Replies
  stream with their tool steps (running, done, failed, stopped) and
  reasoning, can be stopped, edited and resent, regenerated with branches and
  copied. Attachments by picker, drag and drop or paste, images previewed.
  Markdown with tables and highlighted code, and download chips for files the
  agent makes. Read-only share links for signed-in people of the deployment.
- An agent picker with suggestions from `AGENTS.md`, an account page (name,
  password, theme), a connections page, light and dark themes, a phone layout,
  keyboard access and screen reader announcements. Administrators reach the
  Console from the account menu.
- Branding from the hub's `branding/` folder: name, logo, favicon and an
  optional `custom.css`. A gateway uses its own branding and `--name`.

### Sign-in and accounts
- Hubzoid owns accounts in its operational store. Sign-in is off by default
  (local mode: the local owner `admin@localhost`, on loopback only).
  `HUBZOID_AUTH=true` turns it on. The 1.0 names `WEBUI_AUTH`, `WEBUI_URL`,
  `WEBUI_ADMIN_EMAIL` and `WEBUI_ADMIN_PASSWORD` still work.
- Passwords (Argon2id, 8 to 1024 characters), Google, Microsoft (Entra ID) and
  one standard OpenID Connect provider, with Open WebUI's variable names and
  callback paths. Authorization code flow with PKCE, state and nonce, and ID
  tokens checked against the provider's keys. Links to an existing account by
  email only for a verified email with `OAUTH_MERGE_ACCOUNTS_BY_EMAIL=true`.
  `OAUTH_ALLOWED_DOMAINS` applies to every provider.
- Self sign-up (`ENABLE_SIGNUP`, `ENABLE_OAUTH_SIGNUP`) is off by default and
  waits for an administrator's approval.
- One-time sign-in links (72 hours, `HUBZOID_LINK_HOURS`, single use) for new
  accounts and password resets, from the Console's People screen and from
  `hubzoid admin`. Nothing is emailed.
- Sessions: an opaque cookie (`hz_session`) stored as a digest, 30 days at
  most (`HUBZOID_SESSION_DAYS`) and 7 idle days (`HUBZOID_SESSION_IDLE_DAYS`).
  Password, role and status changes, blocks and deletion end sessions.
- Rate limits per client address and per email: 10 failures in 15 minutes
  (`HUBZOID_AUTH_MAX_FAILURES`) lock for 15 minutes, shared by every bridge.
- `HUBZOID_PUBLIC_URL` and `HUBZOID_ALLOWED_ORIGINS` name the addresses people
  use. Changes from a browser must come from one of them.
- New `hubzoid admin create | reset-password | list | set-role` for the
  server's operator. `--owner` gives the owner's access on every hub.
- The first administrator can come from `HUBZOID_ADMIN_EMAIL` and
  `HUBZOID_ADMIN_PASSWORD` on a deployment with no accounts.
- Sign-in events appear in the Console's Activity.

### Chat backend
- Runtimes emit typed run events (text, tool calls and results, reasoning,
  notices). The OpenAI-compatible `/v1` output is byte for byte the 1.0.x
  text, checked against recorded output of every runtime.
- Replies run in a server task that outlives the page: closing the browser
  does not stop a reply, Stop does. A reloaded page follows a running reply.
  One reply per conversation at a time.
- Titles from one model call (the hub's model, or `HUBZOID_TITLE_MODEL`),
  recorded as background usage.
- Limits: `HUBZOID_MAX_UPLOAD_BYTES` (25 MiB per file) and
  `HUBZOID_MAX_FILES_PER_MESSAGE` (10).
- Download links in the web app expire after 7 days by default
  (`HUBZOID_ARTIFACT_LINK_TTL`, `0` for never), and the signed-in owner of a
  conversation can always download its files. Legacy mode is unchanged.
- Workflows: `hub.call_agent` returns only the agent's answer. Tool lines,
  reasoning, download footers and error markers no longer appear in workflow
  data or reports.
- `MODEL=hubzoid-test/<script>` with `HUBZOID_TEST_RUNTIME=1` runs a scripted,
  model-free runtime for tests.

### Personal MCP connections
- Organization administrators register remote MCP servers under **Console →
  Connectors**. Each person connects their own account on **Account →
  Connections** through Hubzoid's OAuth flow (discovery, dynamic client
  registration or a client registered in advance, PKCE, resource indicators).
  Tokens are encrypted with the deployment key and refreshed before they
  expire, one refresh at a time across the deployment.
- Every chat turn and workflow run on every runtime reaches the person's
  servers with their token. `connector_<id>` gates use on Console-managed
  hubs. The connection journey (`HUBZOID_CONNECT_JOURNEY`) works with these
  connectors.

### Groups
- **Console → Groups**: create groups, add and remove members, and see each
  group's access. Groups can be given agent access and capabilities in the
  access editor (never Manage access). Grants name `group:<id>`.
- Group grants apply on every surface that knows the person's email. With
  `SLACK_IDENTITY_MAPPING=true`, Slack senders map to their Hubzoid account and
  its groups.

### Gateway
- `hubzoid gateway` runs one bridge per hub and one edge, with no Open WebUI.
  Bridges share the operational store, so one sign-in covers every agent a
  person may use. Hub-scoped calls go to `/b/<slug>/api`.
- Sign-in is set once in the gateway's environment. A hub `.env` that sets a
  different `HUBZOID_UI`, `HUBZOID_AUTH` or `HUBZOID_SECRET_KEY` stops the
  gateway.
- The deployment key (`secret.key` next to the manifest, or
  `HUBZOID_SECRET_KEY`) is created before the bridges start and its
  fingerprint printed.
- The bridge trusts identity headers in the default mode only with a signed
  assertion from another Hubzoid process (the Slack adapter, the inbound
  process). The edge answers 404 for the bridge's `/v1`, `/uploads` and
  `/otel`.

### Hosted MCP
- OAuth only. Clients connect with Hubzoid-issued OAuth credentials after
  sign-in and consent (`hub:access` scope, access tokens up to 10 minutes,
  refresh tokens and grants up to 30 days, revocable at
  `/mcp/oauth/connections`). Open WebUI API keys no longer authenticate
  `/mcp`. `MCP_PUBLIC_URL` is required with `MCP_SERVER=true`.
- On by default for a local `hubzoid run` and for an https
  `HUBZOID_PUBLIC_URL`. `hubzoid run` prints the `claude mcp add` line.
  `MCP_SERVER` and `MCP_PUBLIC_URL` set in the hub's `.env` still win.

### Moving from Open WebUI
- New `hubzoid migrate openwebui [PATH]` moves people (passwords included),
  external sign-in links, groups, access, conversations with branches and
  attachments, and share links. Open WebUI is only read. The default is a dry
  run. `--apply` writes in steps and undoes them on failure. Re-runs are safe.
  Options: `--owui-db`, `--json`, `--verbose`, `--rehearse`,
  `--model-alias OLD=AGENT`, `--grants auto|people`.
- With sign-in on, a hub or gateway with Open WebUI accounts and no Hubzoid
  accounts does not start until it is moved or put in legacy mode.

### Install and run
- `pip install hubzoid` no longer installs Open WebUI or PyTorch. Measured on
  macOS arm64 with Python 3.12.6 and fresh caches, on a machine shared with
  other work (load average 14 to 21 on 8 cores): a cold `uv` install took
  53 s for 136 packages and a 511 MB environment, a cold `pip` install 202 s
  for 137 packages and 646 MB. The legacy `hubzoid[openwebui]` took 355 s for
  285 packages and 2.1 GB with `uv`.
- Open WebUI stays available for this release as legacy mode:
  `pip install "hubzoid[openwebui]"` and `HUBZOID_UI=openwebui`.
- New required dependencies: pwdlib (argon2 and bcrypt), Authlib,
  itsdangerous and python-multipart. The shared packages Open WebUI used to
  pin exactly (openai, mcp, FastAPI, pydantic and others) keep those release
  lines in the core install for this release. aiohttp is left to LiteLLM, so
  Mac installs get wheels.
- `hubzoid run` serves the web app, file downloads and MCP on one port, prints
  one ready line with the URL and opens it in a browser from a terminal
  (`--no-open` to skip). With sign-in off a network `--host` is refused unless
  `HUBZOID_ALLOW_UNAUTHENTICATED_NETWORK=true`. Measured from the installed
  wheel at load average 12 to 19, the page answered 20 s after a first start
  and 10.5 to 11.2 s after later starts.
- `hubzoid init` scaffolds an operations assistant for a fictional shop by
  default (`--template minimal` keeps the 1.0 starter), and in a terminal
  without a signed-in Claude Code or Codex CLI it offers to save an
  OpenRouter, Anthropic or OpenAI key.
- `hubzoid doctor` reports the web app mode (`ui.mode`), sign-in
  (`auth.chat_signin`), the deployment key by fingerprint (`deployment.key`),
  the `openwebui` extra in legacy mode (`ui.openwebui_extra`), an Open WebUI
  install not yet moved (`ui.openwebui_data`) and the local-mode loopback
  guard (`exposure.local_mode`).
- The Docker image no longer carries ffmpeg, PyAV build tools or PyTorch.
  `--build-arg WITH_OPENWEBUI=true` builds the legacy image. The compose file
  turns sign-in on, since its port is reachable from other machines.
- `import hubzoid` no longer loads the agent SDKs, so the CLI and the edge
  start faster.
- Every package under `hubzoid/` ships: packages are discovered instead of
  listed by hand, and a test fails when a directory of Python modules would not
  ship.

### Development
- Pull requests and pushes to `main` run fast checks again
  (`.github/workflows/tests.yml`): unit tests on SQLite without Open WebUI,
  browsers, live models or Docker, and the web app lint. Publishing a release
  still runs the full validation, now with the `openwebui` extra installed.
- A hygiene test keeps key files, local paths and unapproved customer names
  out of the tracked tree.

### Fixes
- Hubs sharing one PostgreSQL workflow database no longer collide on markdown
  schedule tasks and scheduled evals. Run ids now name the hub
  (`md:<task>:<slot>@<hub>`), so two hubs with the same task and slot both run.
  Runs queued under the earlier ids are still listed, re-queued and cancelled.
- Backups leave the deployment key (`secret.key`) out unless secrets are
  requested, and restoring in place keeps the current key and link secret.
- `.webui_secret_key` is no longer tracked in the repository.

### Webhook workflows
- `@workflow(on_webhook="name")` starts a code workflow from a named webhook
  declared in `workflows/settings.yaml`, served by the hub's own bridge at
  `/webhooks/<hub>/<name>`, in single-hub and gateway modes. Several webhooks per
  hub, each with its own `WEBHOOK_SECRET_<NAME>`, and a shared-secret header or a
  timestamped HMAC (five-minute window). Bodies are limited to 256 KiB.
- A 200 means the event is stored. Each event is one durable record keyed by
  hub, webhook and sender event key (`event_key`, then delivery-id headers,
  then the body hash). Repeats attach to it, and different content under the
  same key answers 409 and raises an alert.
- Hubzoid retries a failed event itself (`webhook_retry_delays`, default 60 and
  300 seconds, three attempts) and then marks it failed with an alert.
  `hubzoid schedule redrive <event-id>` retries it explicitly and
  `hubzoid schedule deliveries` lists events and alert deliveries. Cancelling a
  webhook run fails its event for an explicit redrive.
- After a code change, events that never started run on the current code,
  matched by webhook name. Events whose attempt began under the old code are
  failed visibly, never replayed on changed code.
- `concurrency=N` and `concurrency_key="field.path"` limit runs per workflow and
  per key (one ticket at a time). `max_executor_threads` (default 32) bounds the
  hub's worker threads. A backlog on one webhook workflow does not delay others.
- The run reads its event as `hub.event` (`id`, `key`, `body`, safe headers,
  `webhook`, `received_at`, `attempt`).

### Workflow engine
- One engine owner per hub: a file lock on SQLite and an advisory lock on
  PostgreSQL, with a fenced lease and readiness that webhook admission checks.
  `hubzoid schedule run` hands its run to the live owner, or owns the hub for
  that one run and serves only that workflow's queue.
- A bridge that finds another owner keeps chat running and takes over when the
  owner releases the hub, retrying every 15 seconds.
- A PostgreSQL owner that loses its database session stops claiming work at
  once and keeps chat running. Runs it had claimed stay recoverable instead of
  failing, and the bridge regains ownership by itself.
- Deadlines: `@workflow(timeout=...)` and `workflow_timeout`, 15 minutes by
  default for webhook runs. `hub.call_llm`, `hub.call_agent` and `hub.call_jev`
  take a `timeout` (defaults 120 s, 10 min and 90 s) on every runtime. A run that
  returns just after its deadline keeps its result and raises a timeout alert.
- The workflow engine pins DBOS 3.1.0.

### Alerts and health
- Durable, retried alerts (up to five attempts, `Idempotency-Key`, optional
  `X-Hubzoid-Signature` with `HUBZOID_ALERT_SECRET`) to a webhook, Slack or
  email, per hub (`alerts.to`), per workflow (`alert_to=`) or per markdown task
  (`alert_to:`), with `HUBZOID_ALERT_URL` as the deployment fallback.
- Alerts for failed runs, failed scheduled eval suites, overdue runs, failures
  in a row (`failures_in_a_row`, default 3), a schedule that stopped firing,
  event content mismatches and stale engines, with a one-hour cooldown.
- A schedule that fails 20 scheduled runs in a row pauses itself
  (`pause_after_failures`, `0` turns it off). Manual and webhook runs do not
  count. Webhook workflows are never paused.
- Alert messages carry only the hub, kind, workflow, run id, a count and a
  Console link. Outputs and exception text stay on the server.
- The engine reads only runs finished since its last check, by completion time,
  so monitoring cost does not grow with history.
- The edge watches every hub's engine, alerts when one goes stale and when it
  recovers, and serves `GET /healthz/workflows` (503 while any hub is unhealthy)
  for an external uptime monitor.

### Evals
- Multi-turn cases (`## Turn 1`, `## Turn 2`) in one chat, with one deadline
  for the whole case.
- Tool calls are recorded with arguments, outcome, duration and a 500-character
  preview on every runtime. `expect_tool_args` checks arguments and
  `hubzoid eval run --details` prints the calls.
- `run_as:` in a case, or `--run-as`, runs it as an account. It grants nothing.
- Results are private, atomically written schema 2 files with a locked index
  and retention (`HUBZOID_EVAL_KEEP_RUNS`, default 200). Schema 1 files still
  read.
- A read-only Evals tab on each agent's page. Details of a `run_as` run are
  visible only to that account. Scheduled suites with failing cases now end as
  failed runs, so they raise alerts.

### Agent tools for workflows and access
#### Added
- Workflow tools for agents: `list_workflows`, `workflow_runs`,
  `run_workflow`, `pause_workflow`, `resume_workflow` and
  `cancel_workflow_run`. Two capabilities control them: See workflows and runs
  (`workflows_view`) and Run and control workflows (`workflows_manage`,
  sensitive). They act only on the agent they run in. A run started from chat
  acts as the workflow's own account and is audited as `run_start` with the
  person and surface. Off for everyone until granted.
- Access tools `who_has_access` and `explain_access`, next to the existing
  proposal tools.
- Console: **Hubzoid tools** shows sections, **Workflows** and **Access
  control**, in the access and new-account drawers. Activity shows runs started
  from chat.
- Capabilities can declare a `section`, a `probe` that says why they can't run
  in a hub (for example "No workflows in this agent"), and several switches.
- `hubzoid/workflows/control.py`: run, pause, resume and cancel in one service
  used by the CLI and the tools.

#### Changed
- The access tools need the Manage access from chat capability (`access_tools`,
  granted by organization administrators) instead of `HUBZOID_MANAGEMENT_TOOLS`.
  `HUBZOID_ACCESS_TOOLS=false` and `HUBZOID_WORKFLOW_TOOLS=false` remove a family
  from an agent.
- MCP `tools/list` hides gated built-in tools from callers who may not use them
  (calls were already refused).
- Pause, resume and cancel audit rows record the surface.

#### Deprecated
- `HUBZOID_MANAGEMENT_TOOLS=true` keeps its 1.0.x meaning (every manager gets
  the access tools without a grant) for this release only. `hubzoid doctor`
  warns. Grant `access_tools` instead.

### Compatibility
- `on_failure` now goes through the alert outbox: its POST body is the alert
  payload, with no `error` field, the one-hour cooldown applies, and a value
  that is not an `http(s)://` URL is read as an environment variable name
  holding the URL (1.0.x only logged it).
- `workflows/settings.yaml` is validated when the bridge starts. An invalid file
  turns workflows off for that hub with the error in workflow health. Chat and
  other hubs keep running.
- Webhook names `whatsapp`, `telegram` and the hub's `WEBHOOK_INBOUND_NAME`
  (default `webhook`) are refused, since the inbound server already serves them.
- On PostgreSQL, a new engine owner waits for the previous owner's lease to
  lapse, so after a crash workflows resume up to 90 seconds later. A clean stop
  releases it at once.

### Known limits
- Per-address sign-in limits depend on `X-Forwarded-For` from a TLS proxy.
  Without one, a client can send its own. Per-email limits always apply.
- A reloaded page follows a running reply by polling. There is no stream
  resume.
- Personal connections need a provider with dynamic client registration or a
  client registered in advance. `private_key_jwt` and provider-specific
  authorization parameters are not supported.
- Microsoft emails count as verified only with the `xms_edov` claim. GitHub,
  LDAP and trusted proxy headers are legacy-mode only.
- Arbitrary synchronous workflow code cannot be stopped at its deadline. It
  keeps its thread and ticket partition until it returns.
- Event and alert records have no automatic retention yet.
- The release notes list the remaining limits.

## [1.0.3]

- Fix README images and documentation links on PyPI with absolute URLs.
  Brand images use a fixed source revision so published descriptions retain
  the reviewed artwork.

## [1.0.2]

- CI runs only when a GitHub release is published; pushes and pull requests
  no longer start automated checks. Full validation still runs for releases.
- Docker builds and startup checks run concurrently on native Intel and ARM
  runners, with architecture-specific caches and reuse of published image cache.
- Image, PyPI, and GitHub attachment publishing are separate jobs so failed
  publishing can be retried without repeating successful jobs.
- Existing GitHub releases receive the validated package files automatically;
  publishing retries tolerate packages that are already on PyPI.

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
- **Add user**, on an agent's Access page and on People, creates a new user
  (name, email and a typed or generated password to share manually) with their
  first access in one flow. The password is shown once and is never stored or
  logged. "Google sign-in only" sits beside the password; when Google sign-in
  and `OAUTH_MERGE_ACCOUNTS_BY_EMAIL=true` are configured it creates the
  account with no password to share, otherwise it is disabled with a hint. A
  duplicate email changes nothing and links to that user, whose access is
  edited instead. A partial result keeps the account and offers "Try again"
  without creating a second one. On People, initial access uses the same
  grouped, collapsible capability sections as Edit access, one per agent.
  Public sign-up stays closed.
- A user's details under People show their name, email, status, role and
  access by agent, with **Edit access** per agent and **Add an agent** for an
  agent they can't use yet. Organization administrators approve pending
  sign-ups, reset passwords (a Google-only user's password is managed through
  Google) and delete users from there.
- One **Administrator** role: making someone an Administrator sets Hubzoid
  organization administration and the chat app's admin role together, and
  **User** clears both. A change that sets only one side is reported with
  **Try again** (`502 role_partial` from the API), a user who is an
  administrator on only one side shows **Needs attention** and is never
  promoted implicitly, and the last administrator can't be demoted or deleted
  on either side.
- **Delete user** (in the user's **…** menu, with the email typed to confirm)
  removes every grant, then the chat account and its chats. Activity history,
  usage records and published artifacts are kept. The Console no longer offers
  Block or Reactivate; a user blocked earlier stays blocked and can be unblocked
  through the management API.
- Public email and SSO sign-up default to off (`ENABLE_SIGNUP`,
  `ENABLE_OAUTH_SIGNUP`). Explicit operator overrides remain.
- One authorization service decides every Console and API change. A delegate
  (Manage access on specific agents) grants or removes only what they hold in
  that agent, never Manage access itself. They cannot change their own access
  or an org admin's, and cannot approve, reset, change roles or delete
  accounts. They can create a normal account with access in the agents they
  manage.
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
  its Users tab opens Groups. When every agent is managed in the Console, the
  whole Users section, Groups included, is hidden and opens Settings >
  Integrations. Only Open WebUI administrators see the Admin Panel.
- The Console works at phone and tablet widths: pages don't scroll sideways,
  tables scroll within their frame, drawers take the full width on phones with
  their buttons in reach, and links, small buttons and icons have larger touch
  targets. Agent cards keep their metrics and buttons aligned when a name or
  badge wraps.
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
