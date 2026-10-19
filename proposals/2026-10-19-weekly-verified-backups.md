# Weekly verified backups, PostgreSQL in the archive, and restore rehearsal

Status: proposed for 2.0. Part of HUB-109. Founder decisions of 19 Oct 2026
(B039): weekly verified backups by default, with a changeable schedule;
extend `hubzoid/backup.py` with a verify step (database integrity and file
checksums), PostgreSQL through `pg_dump`, and a rehearsed restore. Run view,
traces and upgrade fencing wait for milestone D.

## Problem

- **Nothing backs up by itself.** Backups exist only if an operator writes a
  cron job. A hosted deployment has no cron.
- **A backup is never checked.** An archive is written and recorded as the
  last backup without being read back. A damaged archive is found at restore
  time, when it is too late.
- **PostgreSQL is left to the operator.** `hubzoid backup` names a PostgreSQL
  database and says to run `pg_dump` by hand.
- **A restore is never practised.** The only way to know a backup restores is
  to restore it over the deployment.

## What should happen

- **Verify.** Every file's SHA-256 goes in the archive's index. After every
  backup the archive is read back: every checksum, `PRAGMA integrity_check`
  on each database and `pg_restore --list` on each dump. A backup that fails
  is deleted and recorded as failed, and the last good one still stands.
  `hubzoid backup verify ARCHIVE` runs the same check later.
- **PostgreSQL.** `pg_dump --format=custom` of each distinct database goes in
  the same archive, under the same schedule hold. The password goes in
  `PGPASSWORD`, never on a command line. `hubzoid restore --postgres` loads it
  back into the database named by an environment variable.
- **Rehearsal.** `hubzoid restore ARCHIVE --rehearse` restores into a scratch
  folder (and a temporary database on the PostgreSQL server named by
  `--postgres-env`, dropped afterwards),
  upgrades each operational store to this release, reports what came back
  and removes the scratch copy. The deployment is never touched.
- **Weekly by default.** The bridge looks every half hour and backs up when
  the last verified backup is older than `HUBZOID_BACKUPS` (`weekly`,
  `daily` or `off`) into `<data>/.hubzoid/backups`, keeping
  `HUBZOID_BACKUP_KEEP` (4). A service that slept through its slot backs up
  after it wakes. A failure waits six hours before the next try. In a gateway
  only the first hub's bridge takes them.
- **Doctor.** `backup.age` shows the last verified backup and the schedule,
  or why the last backup failed.
- **Docs.** `docs/BACKUP.md` covers checking, rehearsal, PostgreSQL, the
  schedule and copying archives elsewhere (restic, rclone). Neither hosting
  provider gives a copy you can take elsewhere.

## Scope and non-goals

- No operations tables, operations API, Console backup screen or write
  barrier across databases (trimmed). The existing schedule hold stays.
- No off-site upload built in. Copying archives is the operator's, with a
  documented recipe.
- No new required hub files or fields.

## Definition of done

- A backup is recorded only after it verifies. A changed file, a cut archive
  and a damaged database each fail verification by name.
- Scheduled backups run when due, are verified, pruned to the kept count and
  never include earlier archives.
- A PostgreSQL deployment backs up, verifies, rehearses into a scratch
  database and restores, against a real PostgreSQL.
- A rehearsal changes nothing in the deployment.
