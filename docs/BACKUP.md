# Backup and restore

`hubzoid backup` saves a deployment's state to one archive while the
deployment keeps serving chat, and checks the archive before it counts.
`hubzoid restore` puts it back, on the same machine or a new one, and can
rehearse that first without touching anything. Every deployment also takes a
verified backup by itself once a week ([Scheduled backups](#scheduled-backups)).

```bash
hubzoid backup ./my-hub --out /var/backups/hubzoid-$(date +%F).tar.gz
hubzoid backup verify /var/backups/hubzoid-2026-09-25.tar.gz
hubzoid restore /var/backups/hubzoid-2026-09-25.tar.gz --rehearse
hubzoid restore /var/backups/hubzoid-2026-09-25.tar.gz
```

## What a backup contains

| Saved | Where it comes from |
|---|---|
| Every SQLite database | The operational store (accounts, sessions, conversations, grants, audit, usage, workflow state and encrypted connector credentials), and each hub's DBOS run history. From a 1.x Open WebUI deployment, also `webui.db` and its vector store, so a pre-2.0 backup stays restorable. Each database is copied with SQLite's online backup. |
| Chat UI data | Hubzoid uploads under each hub's `.hubzoid/chats/`. From a 1.x Open WebUI deployment, also uploads and settings in `<hub>/.openwebui-data` or the gateway's `--data-dir`, but not its model cache. |
| Hub runtime state | Each hub's `.hubzoid`, `.inbound`, `logs` and `output` (files the agent made for users), from its data folder when `HUBZOID_DATA_DIR` is set. |
| PostgreSQL databases | With `DATABASE_URL`, `HUBZOID_OPERATIONAL_DB` or `HUBZOID_DBOS_DB` on PostgreSQL, a `pg_dump` of each distinct database ([PostgreSQL](#postgresql)). |

A hub that belongs to a gateway is backed up with the whole gateway: every hub
in it and the gateway's data directory. They share one operational database,
so they are saved together.

Not saved:

- **Hub content.** `AGENTS.md`, skills, knowledge and tools belong in the
  hub's git repository.
- **Secrets.** Each `.env`, the deployment encryption key (`secret.key` in
  the gateway data directory, or `<hub>/.hubzoid/secret.key`), the artifact signing key
  (`.hubzoid/artifact_secret`) and a 1.x `.webui_secret_key` are left out unless you
  pass `--include-secrets`. So are the database passwords in the gateway's
  `deployment.json`, which is saved with each password replaced by `***`.
  Keep those files, or the configured `HUBZOID_SECRET_KEY`, protected separately.
  Without the deployment key, restored connector credentials (people's
  connections, Shared keys and OAuth client secrets) cannot be decrypted. Without the artifact signing key, old download links stop working.
- **Earlier backups.** The scheduled backups folder is never inside the next
  archive.

The archive is written with owner-only permissions. It still holds user
accounts, password hashes, chats and model connection settings, so store it
like a secret.

## What happens to running work

A backup holds new scheduled runs, for every hub in the deployment, and waits
for queued and running ones to finish. It first pauses five seconds, so a run
that was being queued as the hold began is seen and waited for too. Chat,
Slack, MCP and webhooks keep working. When the backup ends, the hold is
released and any task that fell due meanwhile runs once.

- `--wait 600` (the default) is how many seconds to wait for running work. If
  runs are still going after that, the backup stops and names them. Cancel
  them with `hubzoid schedule cancel`, try later, or pass `--wait 0` to take
  the backup anyway, with no pause and no waiting. A run caught mid-way is
  reported as interrupted after a restore and runs again at its next slot.
- If the backup process dies, the hold expires on its own after the wait time
  plus three hours.

## Checking a backup

Every backup is read back before it counts: each file against the SHA-256
recorded when it was written, an SQLite integrity check of each database and
a listing of each PostgreSQL dump. A backup that fails is deleted, `hubzoid
doctor` reports why (`backup.age`), and the last good one still stands.

Check an archive again at any time, for example after copying it elsewhere:

```bash
hubzoid backup verify hubzoid-2026-09-25.tar.gz          # exits 1 when it fails
hubzoid backup verify hubzoid-2026-09-25.tar.gz --json
```

An archive made by an earlier release has no checksums. Its databases are
still checked, and `verify` says the rest could not be.

## Rehearsing a restore

`--rehearse` restores an archive into a scratch folder, upgrades each copy of
the operational store to this release and reports what came back (people,
conversations, messages), then removes the scratch folder. The deployment is
never touched, so rehearse on the live machine or a spare one, before an
upgrade or once a quarter:

```bash
hubzoid restore hubzoid-2026-09-25.tar.gz --rehearse
```

For an archive with a PostgreSQL dump, give a scratch database to restore it
into. Its URL is read from an environment variable, never the command line,
and whatever that database holds is replaced:

```bash
export SCRATCH_DATABASE_URL=postgresql://hubzoid@localhost/hubzoid_rehearsal
hubzoid restore hubzoid-2026-09-25.tar.gz --rehearse --postgres-env SCRATCH_DATABASE_URL
```

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

Restore checks the archive before it changes anything. It refuses an archive
that would restore to a place a backup never saves from, such as the home
directory or a folder other than a hub's state folders, chat UI data or the
gateway's data directory.

If the gateway's `deployment.json` held database passwords, the restored file
has `***` in their place and restore says so. `hubzoid gateway` rewrites that
file from its environment every time it starts, so start the gateway before
its bridges, or put the passwords back by hand.

### Restoring to a different path or machine

`--move OLD=NEW` restores everything saved under `OLD` to `NEW` instead. It
can be given more than once.

```bash
hubzoid restore backup.tar.gz --move /root/Hubs=/srv/hubs
```

Restore then rewrites the absolute paths that Hubzoid stores: the gateway's
deployment manifest, each hub's pointer to it, the schedule state and, in a
1.x Open WebUI backup, its uploaded-file paths. Check out the hubs' git
repositories at the new paths, copy back each `.env` and the separately saved
deployment encryption and artifact signing keys, then start.

After a restore, run `hubzoid doctor` and open Settings to check access
and recent runs.

## PostgreSQL

With `DATABASE_URL`, `HUBZOID_OPERATIONAL_DB` or `HUBZOID_DBOS_DB` pointing at
PostgreSQL, `hubzoid backup` runs `pg_dump` once per distinct database and
puts each dump in the same archive (`postgres/<n>.dump`). The password reaches
`pg_dump` through its environment, not its command line.

- `pg_dump` must be the server's major version or newer. The Hubzoid image
  ships PostgreSQL 17's client. Elsewhere, install the client tools or set
  `HUBZOID_PG_BIN` to their folder.
- The dumps are taken while new scheduled runs are held, like the rest of the
  archive. Chat keeps working, so a message sent during the backup may be in
  a dump and not in the files, or the reverse. For an exact match, stop the
  deployment and run `hubzoid backup --wait 0`.

To restore, stop the deployment and load the dump into the database named by
an environment variable (default `DATABASE_URL`), with the files:

```bash
hubzoid restore hubzoid-2026-09-25.tar.gz --postgres
hubzoid restore hubzoid-2026-09-25.tar.gz --postgres --postgres-env HUBZOID_OPERATIONAL_DB
```

`pg_restore` replaces what that database holds in one transaction, so a
failure leaves it as it was. An archive with several PostgreSQL databases
(a gateway with separate operational and DBOS databases) is loaded one dump
at a time with `pg_restore --clean --if-exists --no-owner --dbname ...`.

## Scheduled backups

Every deployment takes a verified backup by itself, weekly by default. The
bridge looks every half hour and backs up when the last verified backup is
older than the schedule. A hosted service that slept through its slot backs
up soon after it wakes. In a gateway, the first hub's bridge takes them.

| Setting | Default | What it does |
|---|---|---|
| `HUBZOID_BACKUPS` | `weekly` | `weekly`, `daily` or `off`. |
| `HUBZOID_BACKUP_KEEP` | `4` | How many scheduled archives to keep. Older ones are removed. |
| `HUBZOID_BACKUP_DIR` | `<data>/.hubzoid/backups` | Where they go: the hub's `.hubzoid` folder, or the data folder's when `HUBZOID_DATA_DIR` is set. |

```bash
hubzoid backup list ./my-hub          # newest first, with sizes
```

A failed scheduled backup is recorded and tried again six hours later.
`hubzoid doctor` shows the last good backup, or why the last one failed.

### Copy backups elsewhere

A backup on the same disk protects against a bad change or a damaged file,
not against losing the disk or the volume. Copy the archives off it. With
[restic](https://restic.net) (encrypted, with its own retention):

```bash
restic -r s3:s3.amazonaws.com/my-bucket/hubzoid backup /data/.hubzoid/backups
restic -r s3:s3.amazonaws.com/my-bucket/hubzoid forget --keep-weekly 12 --prune
restic -r s3:s3.amazonaws.com/my-bucket/hubzoid check --read-data-subset=10%
```

or with [rclone](https://rclone.org) to any storage it supports:

```bash
rclone copy /data/.hubzoid/backups remote:hubzoid-backups
```

Keep the deployment key (`HUBZOID_SECRET_KEY`, or `secret.key`) apart from
the archives. A hosted deployment should set `HUBZOID_SECRET_KEY` in the
provider's secrets, so a new volume restored from an archive can still read
stored connector credentials.

On a hosting provider, its own volume snapshots can add to these: Railway
offers scheduled volume backups (kept inside Railway, on some plans only).
Maritime documents none. Neither gives you a copy you can take elsewhere, so
keep copying the archives.

### Without the built-in schedule

Turn it off (`HUBZOID_BACKUPS=off`) and back up from cron instead, for
example daily with two weeks kept:

```bash
#!/bin/sh
# /etc/cron.daily/hubzoid-backup
set -e
cd /srv/hubs
/srv/hubs/venv/bin/hubzoid backup ./alpha --out /var/backups/hubzoid-$(date +%F).tar.gz
find /var/backups -name 'hubzoid-*.tar.gz' -mtime +14 -delete
```
