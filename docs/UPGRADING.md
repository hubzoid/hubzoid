# Upgrading to 1.0.1 from 0.9.x

This release changes security defaults, moves markdown schedules onto the
workflow engine and versions Hubzoid's own database tables. Hub files
(`AGENTS.md`, skills, `schedule/*.md`, `workflows/`) need no changes. Read the
list below once, then follow the steps.

## What changes, and what to do

| Change | What you do |
|---|---|
| The public bridge key `dev` is refused for downloads, and `hubzoid doctor` fails when `BRIDGE_API_KEYS` is unset. | Before restarting, set `BRIDGE_API_KEYS` to a long random value in each hub's `.env` (`openssl rand -hex 32`). The gateway hands each hub's key to the chat app when it starts. Hubzoid refreshes its owned connection URLs and keys at startup, including when native MCP enables persistence. Other saved integrations and settings are preserved. |
| Download links for files the agent made are signed. Links from earlier versions stop working. | Nothing. The files are still there; ask the agent for the link again. Optional expiry: `HUBZOID_ARTIFACT_LINK_TTL`. |
| Administrators can no longer open or export other people's chats in the chat app. | To allow it again, set `ENABLE_ADMIN_CHAT_ACCESS=true` and `ENABLE_ADMIN_EXPORT=true`. |
| The chat app keeps its own branding unless the hub has files in `branding/`. | Nothing, unless you relied on Hubzoid's logo. See [branding.md](branding.md). |
| OpenAI Agents SDK traces are no longer sent to OpenAI. | To keep them, set `HUBZOID_OPENAI_TRACING=true`. |
| Markdown tasks (`schedule/*.md`) run on the hub's workflow engine. Each hub gets `.hubzoid/dbos.db`, and runs appear in the Console. | Nothing. Timing, catch-up and commit behaviour are unchanged. Let running tasks finish before you stop the hub. |
| Hubzoid's tables are versioned and upgraded at the first start. A database written by a newer Hubzoid is refused. | Take the backup in step 4: going back to 0.9.x means restoring it. |
| Tool decisions are stored in the database instead of `logs/access-*.jsonl`. | Nothing. The old files are imported once and left in place. |
| Usage is recorded per chat turn and workflow call (no message content). The Console shows usage totals above the agent cards. | Nothing. Counting starts at the upgrade. |
| In code workflows, `hub.call_llm` is one model call without tools. Earlier it ran the full agent. | If a workflow needs tools there, use `hub.call_agent`. |
| Code-workflow agent calls are not retried unless `agent_max_attempts` is set. Runs left over from older workflow code are cancelled at start. | Drain queued runs before upgrading ([workflows.md](workflows.md)). |
| The public port rejects `.` and `..` path segments and drops client-sent `X-Hubzoid-*` and `X-OpenWebUI-*` headers. | Nothing, unless you relied on those. |

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
6. **Start** as before. At the first start Hubzoid upgrades its tables and
   imports the old decision logs.
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

## First-use stabilization on this branch

The configured, verified owner now receives initial administration and hub entry
once. Existing hubs are not automatically migrated to managed access. Record the
owner email before rollout, back up all stores, and verify owner and ordinary-user
access after restart. Subsequent sign-in preserves revocations.

New workflow scaffolds are manual and model-free. Existing schedules are unchanged.
Chat usage now uses the forwarded chat ID; local Open WebUI background task types
are recorded separately from user messages. Historical usage is not rewritten.

Old DBOS workflow-history migration is not included in this stabilization. Preserve
the old execution database and rehearse the installed DBOS upgrade path on a copy.
An inaccessible old history store is a rollout failure for deployments that need
that history; deleting it is not a migration procedure.
