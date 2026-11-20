# Rolling back

Three things can go back, each on its own:

| What | How | Detail |
|---|---|---|
| One change to the hub's files | `hubzoid hub-changes undo ./my-hub <id>`, or `git revert` | [Approvals and hub changes](approvals-and-hub-changes.md) |
| The hub's files to an earlier commit | Deploy that commit again | Below |
| The Hubzoid version | Restore the backup taken before the upgrade, then install the earlier version | Below |

## The hub's files

A hub is a Git folder, so an earlier version is a commit. Deploy that commit the
way the hub is normally deployed (pull and `hubzoid reload`, or build the image
from that commit). Files are read on reload, and nothing in the operational
store needs to change.

`hubzoid eval run --required` on the earlier commit shows it still passes before
it goes live ([CI](ci.md)).

## The Hubzoid version

An upgrade can change the operational store's schema, and an earlier version
refuses a schema it does not know. So going back is always "restore, then
install":

1. **Before every upgrade**, take a backup and check it:
   `hubzoid backup ./my-hub --out pre-upgrade.tar.gz` and
   `hubzoid backup verify pre-upgrade.tar.gz` ([Backup](BACKUP.md)).
2. **To go back**, stop the hub or gateway, then:

   ```bash
   hubzoid restore pre-upgrade.tar.gz
   pip install "hubzoid==<previous version>"
   ```

   With PostgreSQL, add `--postgres` so the database goes back too
   (`--postgres-env` names its variable when it is not `DATABASE_URL`). A plain
   restore puts back the files only, and the earlier version refuses the newer
   schema ([PostgreSQL](BACKUP.md#postgresql)).

   With an image, run the previous image tag (or digest) on the restored data.
3. Start it and run `hubzoid doctor` and `hubzoid eval run --required`.

Anything people did after the upgrade (new conversations, approvals, runs) is
in the newer store, not the backup. Check the release's
[Upgrading](UPGRADING.md) section for steps that version needs on the way back.

Settings a newer version added are ignored by an older one. A hub that sets
`guardrails:` in `AGENTS.md` runs without guardrails on a version before 2.0.9.

## Work in progress across a switch

Workflow runs belong to the code that started them. After a version change,
runs that were interrupted or queued under the old code are cancelled at the
next start and show as `CANCELLED`. Markdown runs that were queued but not
started are queued again ([Workflows](workflows.md)). A backup waits for queued
and running work to finish before it is taken, and a run caught mid-way runs
again at its next slot after a restore ([Backup](BACKUP.md#what-happens-to-running-work)).

## Rehearse it

Before an upgrade, or once a quarter:

1. `hubzoid restore pre-upgrade.tar.gz --rehearse` restores into a scratch
   folder, upgrades a copy and reports what came back, without touching the
   deployment ([Rehearsing a restore](BACKUP.md#rehearsing-a-restore)).
2. On a spare machine, restore the backup, install the previous version, start
   it and run `hubzoid eval run --required`.
