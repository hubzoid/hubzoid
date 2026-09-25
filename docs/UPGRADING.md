# Upgrading to 1.0.1 from 0.9.x

This release changes security defaults, moves markdown schedules onto the
workflow engine and versions Hubzoid's own database tables. Hub files
(`AGENTS.md`, skills, `schedule/*.md`, `workflows/`) need no changes. Read the
list below once, then follow the steps.

## What changes, and what to do

| Change | What you do |
|---|---|
| The public bridge key `dev` is refused for downloads, and `hubzoid doctor` fails when `BRIDGE_API_KEYS` is unset. | Before restarting, set `BRIDGE_API_KEYS` to a long random value in each hub's `.env` (`openssl rand -hex 32`). The gateway hands each hub's key to the chat app when it starts. If a hub sets `OWUI_NATIVE_MCP=true`, the chat app keeps its own copy of the connection: update the key there too (Admin, Settings, Connections). |
| Download links for files the agent made are signed. Links from earlier versions stop working. | Nothing. The files are still there; ask the agent for the link again. Optional expiry: `HUBZOID_ARTIFACT_LINK_TTL`. |
| Administrators can no longer open or export other people's chats in the chat app. | To allow it again, set `ENABLE_ADMIN_CHAT_ACCESS=true` and `ENABLE_ADMIN_EXPORT=true`. |
| The chat app keeps its own branding unless the hub has files in `branding/`. | Nothing, unless you relied on Hubzoid's logo. See [branding.md](branding.md). |
| OpenAI Agents SDK traces are no longer sent to OpenAI. | To keep them, set `HUBZOID_OPENAI_TRACING=true`. |
| Markdown tasks (`schedule/*.md`) run on the hub's workflow engine. Each hub gets `.hubzoid/dbos.db`, and runs appear in the Console. | Nothing. Timing, catch-up and commit behaviour are unchanged. Let running tasks finish before you stop the hub. |
| Hubzoid's tables are versioned and upgraded at the first start. A database written by a newer Hubzoid is refused. | Take the backup in step 4: going back to 0.9.x means restoring it. |
| Tool decisions are stored in the database instead of `logs/access-*.jsonl`. | Nothing. The old files are imported once and left in place. |
| Usage is recorded per chat turn and workflow call (no message content). The Console opens on a new Overview page. | Nothing. Counting starts at the upgrade. |
| In code workflows, `hub.call_llm` is one model call without tools. Earlier it ran the full agent. | If a workflow needs tools there, use `hub.call_agent`. |
| Code-workflow agent calls are not retried unless `agent_max_attempts` is set. Runs left over from older workflow code are cancelled at start. | Drain queued runs before upgrading ([workflows.md](workflows.md)). |
| The public port rejects `.` and `..` path segments and drops client-sent `X-Hubzoid-*` and `X-OpenWebUI-*` headers. | Nothing, unless you relied on those. |

## Steps

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
   file the agent makes, open the Console (Overview, Runs, Activity), and run
   `hubzoid doctor <hub>` again.

## Going back

Stop everything. Restore with the new version still installed (older versions
have no `restore` command), then reinstall the previous version and start:

```bash
hubzoid restore pre-upgrade.tar.gz
pip install "hubzoid==<previous version>"
```

Whatever the restore replaces is kept beside it as `<name>.pre-restore-<time>`.
