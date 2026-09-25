# R1: one scheduler, dependable workflows, a chat-first Console

Status: agreed with the founder on 2026-09-25. Ships in the same release as the R0
stabilization fixes, on `auth-schedules-upgrade`.

## Problem
- Two engines schedule work today. `schedule/*.md` tasks run on the legacy tick
  loop (`scheduler.py`, `schedule_runner.py`, a file lock and a JSON fire-state),
  and `workflows/*.py` run on DBOS. They have different time zones, missed-run
  rules, locks and histories, and only one of them shows in the Console. Git
  `commit:`/`push:` steps in markdown tasks fail in ways nobody sees until the
  next run.
- `hub.call_llm` is not a model call. It runs the whole hub agent (tools, skills,
  MCP connectors, rebuilt per call), returns free text, and drops keyword
  arguments inside a workflow.
- Hubzoid's own tables are created with `CREATE TABLE IF NOT EXISTS` and have no
  versioning, so the first column change can't reach an existing install.
- There is no backup/restore story, CI runs only after a release is already
  published, and the Console has no overview of how the hubs are being used.

## Why now
The auth + schedules work made the Console and DBOS real. An existing multi-hub
deployment will upgrade to this release, so the schedule cutover, schema
versioning and backup have to be right before, not after.

## What should happen

### Scheduling: one engine, two ways to write work
- `schedule/*.md` stays the default for builders: write markdown, Hubzoid runs it.
  Same files and fields (`schedule`, `on_webhook`, `run`, `commit`, `push`,
  `timeout`, `max_rounds`, `max_turns`, `enabled`).
- `workflows/*.py` is the code path for exact, deterministic steps.
- Both run on DBOS. The legacy tick loop, lock and fire-state are removed.
- Markdown tasks keep today's semantics: machine-local time unless a zone is
  given, one catch-up run after downtime, one markdown task at a time per hub,
  deferred while the hub is serving chat, on whenever the folder has enabled
  tasks (no new setting). The last-fired times are imported at cutover so
  nothing fires twice or is skipped.
- A markdown run executes as separate steps: agent rounds, `run:` script,
  commit (skipped when there is nothing to commit), push (safe to retry). A
  rebase conflict fails the run cleanly and shows in run history instead of
  leaving a broken checkout. Agent rounds and scripts are not retried.
- Names: markdown tasks are `md:<name>` internally, Python workflows keep their
  function name, so the two never collide. The service identity for grants is
  `workflow:<name>` for both.
- Versioning: the DBOS application version covers the Hubzoid version and
  `workflows/**/*.py`. Markdown runs read their task file at run time, so
  `schedule/*.md` is not part of it. On upgrade or edit, code-workflow runs from
  other code are cancelled at start and stay visible as CANCELLED, and markdown
  runs that were queued but not started are queued again. Drain before a
  planned upgrade.
- Python workflows get one queue each (one run at a time per workflow). A
  per-hub cap is available in `workflows/settings.yaml`, not on by default.

### Model and agent calls in workflows
- `hub.call_llm(prompt, *, response_format="text"|"json", response_model=None,
  model=None, system=None)`: one model call, no tools. Text by default, a dict
  for `"json"`, a validated Pydantic object with `response_model`. OpenAI-backend
  models use LiteLLM's JSON mode; `claude-local` runs a single tool-free turn
  through the Claude Agent SDK. Same parsing, errors and network retries on both.
- `hub.call_agent(task, *, response_model=None)`: the full hub agent with tools.
  Not retried unless the hub opts in (`agent_max_attempts`).
- `hub.decide(state, questions, *, model="typesafe/jev-1.13")` (experimental):
  a typed decision with probabilities and confidence from TypeSafe's Jev via
  OpenRouter's decisions endpoint. Needs `OPENROUTER_API_KEY`.
- Call details and raw results are stored so a restarted workflow replays them.
- Idempotency is documented guidance, not a helper that would overpromise.

### Usage recorded by Hubzoid, not by the chat UI
- New table `hz_usage`: one row per completed turn with time, hub, surface (web,
  slack, whatsapp, telegram, api, workflow), user, chat id, model, input and
  output tokens, estimated cost, status and duration. No message content.
- Written by the bridge for every chat turn (so it works behind Open WebUI,
  LibreChat or Assistant UI, and counts Slack/WhatsApp/Telegram) and by
  `call_llm`/`call_agent`/`decide` in workflows.
- Estimated cost: Claude's reported cost; LiteLLM's price table for other
  models; "unavailable" when unknown. Always labelled estimated.

### Console home (read-only)
- Headline numbers for 24h / 7d / 30d: chats, messages, active users, tokens in
  and out, estimated cost, tool denials. Workflow numbers (runs, failed, missed)
  only when a hub has schedules or workflows.
- A per-hub table: chats, active users, messages, tokens, estimated cost, last
  activity, users with access, plus schedule columns where relevant. A row opens
  the hub.
- Data comes only from Hubzoid's database (usage, access, DBOS). Unknown numbers
  read "unavailable", never zero. Hub admins see only their hubs.
- No run controls in the Console. `hubzoid schedule run | cancel | pause | resume`
  act with the authority of whoever can run commands on the server. Pause is
  stored durably; cancel is best effort; every action is audited in the database.

### Storage, backup and delivery
- Alembic manages Hubzoid-owned tables in both stores (the shared operational DB
  and each hub's own DB), with its own version table, a migration lock for
  bridges starting together, recognition of valid partial schemas from existing
  installs, and a hard stop on unknown schemas. Open WebUI and DBOS keep their
  own migrations.
- `hubzoid backup` / `hubzoid restore`: pause new scheduled runs and wait for
  running ones (chat keeps working), then snapshot the SQLite databases, chat UI
  data, `.hubzoid`, `.inbound` and `logs`; secrets only when asked. Restore
  rewrites stored absolute paths. PostgreSQL: documented `pg_dump` route.
- Profiles: SQLite by default (quick start on one machine), PostgreSQL for
  production. Both tested with Docker.
- CI: light on every PR (unit tests, lint, Console build); a release is validated
  from its tag first, then PyPI, the GitHub release and a GHCR image
  (`ghcr.io/hubzoid/hubzoid:<version>`, amd64 + arm64) are published.
- `hubzoid doctor --json` with stable check IDs.

## Scope and non-goals
- Not in R1: run controls in the Console, rich replay, new identity or chat UI
  (R2), per-message content analytics, exactly-once guarantees for external
  effects.

## Open questions
- Jev's OpenRouter endpoint is alpha; `hub.decide` is pinned to one tested request
  shape and marked experimental.
- Usage rows and access decision rows are not pruned in R1; a retention
  setting can follow if needed.

## Definition of done
- An existing deployment with markdown schedules upgrades with no hub file
  changes; every task type passes a parity check; nothing fires twice at cutover.
- A workflow's `call_llm` cannot reach tools and returns validated JSON on both
  backends.
- The Console home shows real per-hub chat and usage numbers from Hubzoid's own
  table with no Open WebUI dependency.
- Backup, restore, fresh install and upgrade pass on SQLite and PostgreSQL.
