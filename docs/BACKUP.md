# Backup and restore

`hubzoid backup` saves a deployment's state to one archive while the
deployment keeps serving chat. `hubzoid restore` puts it back, on the same
machine or a new one.

```bash
hubzoid backup ./my-hub --out /var/backups/hubzoid-$(date +%F).tar.gz
hubzoid restore /var/backups/hubzoid-2026-09-25.tar.gz
```

## What a backup contains

| Saved | Where it comes from |
|---|---|
| Every SQLite database | The operational store (access grants, audit, usage, workflow state), each hub's DBOS run history, Open WebUI's `webui.db` and vector store. Copied with SQLite's online backup, so the copy is consistent even while the hub is running. |
| Chat UI data | Open WebUI uploads and settings (`<hub>/.openwebui-data`, or the gateway's `--data-dir`). Its model cache is left out because it is rebuilt on demand. |
| Hub runtime state | Each hub's `.hubzoid`, `.inbound`, `logs` and `output` (files the agent made for users). |

A hub that belongs to a gateway is backed up with the whole gateway: every hub
in it and the gateway's data directory. They share one operational database,
so they are saved together.

Not saved:

- **Hub content.** `AGENTS.md`, skills, knowledge and tools belong in the
  hub's git repository.
- **Secrets.** Each `.env`, the artifact signing key
  (`.hubzoid/artifact_secret`) and `.webui_secret_key` are left out unless you
  pass `--include-secrets`. Keep your `.env` files somewhere safe on their own.
  Without the signing key, old download links stop working after a restore.
- **PostgreSQL databases.** The backup names them. Use `pg_dump` (below).

The archive is written with owner-only permissions. It still holds user
accounts, password hashes, chats and model connection settings, so store it
like a secret.

## What happens to running work

A backup holds new scheduled runs, for every hub in the deployment, and waits
for queued and running ones to finish. Chat, Slack, MCP and webhooks keep
working. When the backup ends, the hold is released and any task that fell due
meanwhile runs once.

- `--wait 600` (the default) is how many seconds to wait for running work. If
  runs are still going after that, the backup stops and names them. Cancel
  them with `hubzoid schedule cancel`, try later, or pass `--wait 0` to take
  the backup anyway. A run caught mid-way is reported as interrupted after a
  restore and runs again at its next slot.
- If the backup process dies, the hold expires on its own after the wait time
  plus three hours.

## Restoring

Stop the hub or the gateway and its bridges first. Restore refuses to replace
a database that another process is using.

```bash
hubzoid restore backup.tar.gz --dry-run   # where everything would go
hubzoid restore backup.tar.gz
```

Each saved directory goes back to the path it was saved from. Whatever is at
that path now is kept beside it as `<name>.pre-restore-<time>`, so a restore
can be undone by moving those back. Delete them once the restored deployment
is checked.

### Restoring to a different path or machine

`--move OLD=NEW` restores everything saved under `OLD` to `NEW` instead. It
can be given more than once.

```bash
hubzoid restore backup.tar.gz --move /root/Hubs=/srv/hubs
```

Restore then rewrites the absolute paths that Hubzoid and Open WebUI store:
the gateway's deployment manifest, each hub's pointer to it, the schedule
state and Open WebUI's uploaded-file paths. Check out the hubs' git
repositories at the new paths, copy back each `.env`, then start.

After a restore, run `hubzoid doctor` and open the Console to check access
and recent runs.

## PostgreSQL

With `DATABASE_URL`, `HUBZOID_OPERATIONAL_DB` or `HUBZOID_DBOS_DB` pointing at
PostgreSQL, those databases are not in the archive. Back them up with the
standard tools, at the same time as `hubzoid backup`:

```bash
pg_dump --format=custom --file hubzoid-$(date +%F).dump "$DATABASE_URL"
hubzoid backup ./my-hub --out hubzoid-files-$(date +%F).tar.gz
```

To restore, stop the deployment, then:

```bash
pg_restore --clean --if-exists --no-owner --dbname "$DATABASE_URL" hubzoid-2026-09-25.dump
hubzoid restore hubzoid-files-2026-09-25.tar.gz
```

If Open WebUI also uses PostgreSQL (its own `DATABASE_URL`), dump that
database the same way.

## Scheduling backups

A daily backup with two weeks kept:

```bash
#!/bin/sh
# /etc/cron.daily/hubzoid-backup
set -e
cd /srv/hubs
/srv/hubs/venv/bin/hubzoid backup ./alpha --out /var/backups/hubzoid-$(date +%F).tar.gz
find /var/backups -name 'hubzoid-*.tar.gz' -mtime +14 -delete
```

Copy archives off the machine as well. A backup on the same disk does not
survive losing the disk.
