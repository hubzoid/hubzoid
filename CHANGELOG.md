# Changelog

All notable changes to Hubzoid. Versions follow the package version in
`pyproject.toml`; each release tag `vX.Y.Z` must have a section here.

## [1.0.1] - unreleased

Upgrading from 0.9.x: read [docs/UPGRADING.md](docs/UPGRADING.md) first.

### Workflows and schedules
- Markdown schedule tasks (`schedule/*.md`) run on the hub's DBOS engine, with
  the same files, timing and catch-up rules. Each run is split into work,
  commit, push and finish steps; a push is retried, interrupted work is
  reported rather than repeated, and every run appears in the Console. The
  legacy tick loop, file lock and in-process runner are removed.
- `hub.call_llm` is one model call with no tools: text, JSON, or a validated
  Pydantic object (`response_model`), on LiteLLM models and `claude-local`.
  `hub.call_agent` keeps the full agent. `hub.decide` (experimental) asks
  TypeSafe's Jev through OpenRouter for a typed decision.
- Code workflows run one at a time per workflow, side by side across
  workflows. Optional hub-wide cap: `max_concurrent_workflows`.
- `hubzoid schedule pause | resume | cancel`, recorded in the access log. The
  Console shows paused work but has no run buttons.
- Webhooks: GitHub's `X-Hub-Signature-256` signatures are accepted,
  repeated deliveries (by delivery id, or an identical body shortly after) are
  dropped, and a failed sink releases the delivery for a retry.
- New sample: `hubzoid init <name> --template watchtower`.

### Console
- Opens on **Overview**: conversations, messages, active people, tokens,
  estimated cost and tool denials for 24 hours, 7 days or 30 days, then one row
  per agent. Workflow numbers appear only for agents that have them. All from
  Hubzoid's own tables, never the chat app's database.
- Every chat turn and workflow model call writes a usage row (`hz_usage`): time,
  hub, surface, user, chat, model, tokens, estimated cost, status and duration.
  No message content.

### Access
- Tool decisions are stored in the database (`hz_access_decisions`) instead of
  monthly JSONL files, which are imported once. A restricted call whose
  decision cannot be recorded is refused.
- Scheduled markdown runs act as `workflow:md:<task>`, grantable like a person.
- The public port refuses `.` and `..` path segments (they could reach bridge
  paths outside the forwarded routes) and drops client-sent `X-Hubzoid-*` and
  `X-OpenWebUI-*` headers.

### Operations
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
  are cancelled at start instead of blocking the queue.
- Dependencies bounded and locked (`requirements.lock`); Docker image built from
  source with CPU-only PyTorch, publishing only port 3080.
