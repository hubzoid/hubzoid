# Upgrading to 1.0.1 from 0.9.x

This release changes security defaults, moves markdown schedules onto the
workflow engine, versions Hubzoid's own database tables and changes the Docker
image. Hub files (`AGENTS.md`, skills, `schedule/*.md`, `workflows/`) need no
changes. Read the list below once, then follow the steps.

## What changes, and what to do

| Change | What you do |
|---|---|
| Security: the edge no longer shares one visitor's sign-in cookie with another. | After upgrading, rotate `WEBUI_SECRET_KEY` (everyone signs in again) so any session that may have leaked ends. Personal Open WebUI MCP connections must then be reconnected. |
| The public bridge key `dev` is refused for downloads, and `hubzoid doctor` fails when `BRIDGE_API_KEYS` is unset. | Before restarting, set `BRIDGE_API_KEYS` to a long random value in each hub's `.env` (`openssl rand -hex 32`). The gateway hands each hub's key to the chat app when it starts. Hubzoid refreshes its owned connection URLs and keys at startup, including when native MCP enables persistence. Other saved integrations and settings are preserved. |
| Public sign-up is off by default. Hubzoid now sets `ENABLE_SIGNUP` and `ENABLE_OAUTH_SIGNUP` to `false` unless you set them. In 0.9.x the chat app let anyone who reached the sign-in page create an email account unless `ENABLE_SIGNUP=false` was set. | Existing accounts keep working. Add new people in the chat app's admin panel (**Admin Panel → Users**). To allow self sign-up again, set `ENABLE_SIGNUP=true` (email) or `ENABLE_OAUTH_SIGNUP=true` (SSO) explicitly. If the chat app keeps its settings in its database (for example with `OWUI_NATIVE_MCP=true`), check **Enable New Sign Ups** in its admin settings too, because a value saved there earlier can win. See [auth.md](auth.md). |
| Download links for files the agent made are signed. Links from earlier versions stop working. | Nothing. The files are still there; ask the agent for the link again. Optional expiry: `HUBZOID_ARTIFACT_LINK_TTL`. |
| Administrators can no longer open or export other people's chats in the chat app. | To allow it again, set `ENABLE_ADMIN_CHAT_ACCESS=true` and `ENABLE_ADMIN_EXPORT=true`. |
| The chat app keeps its own branding unless the hub has files in `branding/`. | Nothing, unless you relied on Hubzoid's logo. See [branding.md](branding.md). |
| OpenAI Agents SDK traces are no longer sent to OpenAI. | To keep them, set `HUBZOID_OPENAI_TRACING=true`. |
| Markdown tasks (`schedule/*.md`) run on the hub's workflow engine. Each hub gets `.hubzoid/dbos.db` (with PostgreSQL in `DATABASE_URL`, the engine uses that database instead), and runs appear in the Console. | Nothing. Timing, catch-up and commit behaviour are unchanged. Let running tasks finish before you stop the hub. |
| Hubzoid's tables are versioned and upgraded at the first start. A database written by a newer Hubzoid is refused. | Take the backup in step 4: going back to 0.9.x means restoring it. |
| Tool decisions are stored in the database instead of `logs/access-*.jsonl`. | Nothing. The old files are imported once and left in place. |
| Upgrading does not change who can use each agent. Existing hubs keep their Open WebUI group-based access until an operator migrates them to Console-managed access. | Nothing during the upgrade. Do not run `hubzoid access migrate --apply` as part of it. When you are ready, migrate one hub at a time in its own maintenance window, following [Migrate existing customers](ADMINISTRATION.md#migrate-existing-customers). |
| Gateway: a hub's `.env` no longer carries into the shared chat app. Sign-in and chat-app settings (`WEBUI_*` such as `WEBUI_SECRET_KEY` and `WEBUI_URL`, `DEFAULT_USER_ROLE`, `ENABLE_SIGNUP`, OAuth settings, `HUBZOID_PUBLIC_URL`) are still read from the hub `.env` files when the gateway's own environment does not set them. The gateway lists them when it starts. Model keys and restricted-tool secrets no longer reach the chat app, and `WEBUI_NAME` in a hub `.env` no longer overrides `--name`. | Nothing is required. When convenient, move those settings into the gateway's environment (with systemd, its drop-in) so the gateway stops listing them. Keep `WEBUI_SECRET_KEY` unchanged, or everyone is signed out. |
| Gateway: all bridges share one access and usage database, `hubzoid-operational.db` in the gateway's data directory. The gateway points each hub at it when it starts. | If the bridges run as their own services (`--no-bridges`), restart them once after the gateway's first start so they use the shared database. |
| The `claude` CLI and the MCP servers it starts no longer inherit restricted-tool values (`restricted/.env`), Hubzoid service secrets (bridge keys, `WEBUI_SECRET_KEY`, database URLs, SMTP credentials) or AWS credentials. Restricted tools run in the bridge and keep working. | Nothing, unless an MCP server in `.mcp.json` read one of those values from its environment. Give it the value in its own `env` block. |
| Open WebUI no longer receives `HUBZOID_*` settings. Under `hubzoid run`, values from `restricted/.env` no longer reach it either (sign-in settings found there still pass, with a warning). | Keep Open WebUI and sign-in settings in the hub `.env` or the gateway environment. |
| New features are off until enabled: Console account management, agent-proposed access changes (`HUBZOID_MANAGEMENT_TOOLS`), hiding the Open WebUI Users page (`HUBZOID_HIDE_OWUI_USERS`), connection journeys (`HUBZOID_CONNECT_JOURNEY`) and AWS Secrets Manager layers. | Nothing. See [ADMINISTRATION.md](ADMINISTRATION.md), [mcp.md](mcp.md) and [DEPLOYING.md](DEPLOYING.md) to turn them on. |
| The Console's Public access switch is removed, and new "Everyone signed in" grants are refused everywhere, including `hubzoid grant '*'`. Existing ones keep working and show as an "Everyone signed in" row. | Nothing is required. To move to named access, grant the people or groups who need the agent, then remove the "Everyone signed in" row (org admins). |
| `identity/permissions.yaml` can no longer relabel built-in capabilities (Use this agent, Manage access, Save shared knowledge). Such entries are ignored with a warning. | Remove those entries if you had any. Labels for `restricted/` tools still work. |
| On hubs already migrated to Console access, a personal Open WebUI MCP connection is used only by people granted that app's `connector_<app>` capability. | Grant `connector_<app>` to the people who use it. Legacy hubs are unchanged. |
| Gateway: any bridge can serve the Console and the agent picker's access check. While one bridge is down or restarting, the next one answers. | Nothing. |
| Usage is recorded per chat turn and workflow call (no message content). The Console shows usage totals above the agent cards. | Nothing. Counting starts at the upgrade. |
| The public port rejects `.` and `..` path segments and drops client-sent `X-Hubzoid-*` and `X-OpenWebUI-*` headers. | Nothing, unless you relied on those. |
| Docker: the old `docker/Dockerfile` is removed. The image is built from the `Dockerfile` at the repository root, now on Debian 13, and installs the checked-out source. Each release is also published as `ghcr.io/hubzoid/hubzoid:<version>` (amd64 and arm64). | Check out the release tag and run `docker build -t hubzoid .`, or pull the published image. `--build-arg HUBZOID_VERSION` no longer picks the version. For Compose, use `docker/docker-compose.yml` (SQLite) and add `docker/docker-compose.postgres.yml` for PostgreSQL. See [DEPLOYING.md](DEPLOYING.md#path-b-docker). |
| Docker: only the edge port 3080 is published. The bridge port 8000 is no longer published, because the bridge listens only inside the container. | Remove any `8000:8000` port mapping. Chat, downloads, the Console and MCP all go through port 3080. |
| Docker: the image's entrypoint is `hubzoid` with the default command `run /hub`, and it runs as an unprivileged user. The old Compose image ran as root. | If you passed arguments after the image name, pass the whole command now, for example `run /hub --slack`. On Linux, make the mounted hub folder writable by the container user ([DEPLOYING.md](DEPLOYING.md#path-b-docker)). |
| Hubzoid's own code is licensed under Apache-2.0. 0.9.x was MIT. Dependencies keep their own licences. | Nothing to run. If you redistribute Hubzoid, keep [LICENSE](../LICENSE) and [NOTICE](../NOTICE) with it. See [LICENSING.md](../LICENSING.md). |

## Steps

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
5. **Check**:
   ```bash
   hubzoid doctor <hub>
   ```
   Fix every `fail`. `db.operational` shows `info` or `warn` until the first
   start upgrades the schema.
6. **Start** as before. At the first start Hubzoid upgrades its tables. It
   imports the old decision logs the first time it records or reads a tool
   decision. With a gateway whose bridges run as their own services, restart
   the bridges once after the gateway is up.
7. **Verify**: sign in as a normal user and chat with each agent, download a
   file the agent makes, open the Console (Agents, per-agent Runs & schedules, Activity), and run
   `hubzoid doctor <hub>` again.

## Going back

Stop everything. Restore with the new version still installed (older versions
have no `restore` command), then reinstall the previous version and start:

```bash
hubzoid restore pre-upgrade.tar.gz
pip install "hubzoid==<previous version>"
```

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

## First sign-in and usage after the upgrade

The configured, verified owner now receives initial administration and hub entry
once. Existing hubs are not automatically migrated to managed access. Record the
owner email before rollout, back up all stores, and verify owner and ordinary-user
access after restart. Subsequent sign-in preserves revocations.

New workflow scaffolds are manual and model-free. Existing schedules are unchanged.
Chat usage now uses the forwarded chat ID; local Open WebUI background task types
are recorded separately from user messages. Historical usage is not rewritten.

## Only if you ran a pre-release build of 1.0.1

Skip this section if you are upgrading from 0.9.x. Those releases had no
workflow engine and no code workflows, so there is no run history or queued run
to carry over.

If you ran a pre-release build of 1.0.1, also note:

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

## Workflows run as people; reports and email (next release)

Scheduled work now acts as an ordinary account, and can publish private reports
and email its owner ([workflow-identity.md](workflow-identity.md),
[reports-and-email.md](reports-and-email.md)). The database gains the tables for
this at the first start (`op_0005`, forward only: restore a backup to go back).

| Change | What you do |
|---|---|
| Runs act as `run_as`, else `HUBZOID_WORKFLOW_USER` (hub, then deployment), else the owner recorded at setup, instead of `workflow:<name>` / `workflow:md:<task>`. | Run `hubzoid schedule list <hub>`: it shows who each workflow and task runs as. On a Console-managed hub, set `run_as` or `HUBZOID_WORKFLOW_USER` if the default is not the account you want. |
| Grants to `workflow:*` subjects are kept but no longer used. They are never copied to anyone. | For each permission a run's log reports as "not held", grant it to the account the work runs as (or point `run_as` at an account that holds it), then remove the old grant. |
| A legacy hub (access still in the chat app) with no account configured keeps running as before, with a warning. Its runs still reach no restricted tool. | Nothing. Configure an account when you want reports, email or personal connections there. |
| `hub.state` belongs to the account a run acts as. Existing state is adopted by the first account that runs the workflow after the upgrade. A markdown task's scratch folder likewise belongs to the first account that runs it; another account gets its own. | If a workflow's state must survive a change of account, move it to `hub.shared_state` (keep personal data out of it). |
| New permission **Share reports by public link** (`share_public_links`). | Grant it only to people who may create "anyone with the link" reports. |
| Owner email needs `HUBZOID_SMTP_HOST` and `HUBZOID_SMTP_FROM` (plus credentials, over TLS). | Set them in the deployment configuration (or a deployment secret) when you want workflow email. Use `HUBZOID_EMAIL_DELIVERY=preview` to try it without sending. |
