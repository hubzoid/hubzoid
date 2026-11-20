# Rolling back

When a change makes an agent worse, put it back. Three things can go back, each
on its own: one change to the hub's files, all of the hub's files, or the
Hubzoid version.

```bash
hubzoid hub-changes undo ./my-hub <id>
```

## What can go back

| What | How | Detail |
|---|---|---|
| One change to the hub's files | `hubzoid hub-changes undo ./my-hub <id>`, or `git revert` | [Approvals and hub changes](approvals-and-hub-changes.md) |
| The hub's files to an earlier commit | Deploy that commit again | [Roll back the hub's files](#roll-back-the-hubs-files) |
| The Hubzoid version | Restore the backup taken before the upgrade, then install the earlier version | [Roll back the Hubzoid version](#roll-back-the-hubzoid-version) |

## Roll back the hub's files

A hub is a Git folder, so an earlier version is a commit. Deploy that commit the
way the hub is normally deployed: pull and `hubzoid reload`, or build the image
from that commit. Files are read on reload, and nothing in the operational
store needs to change.

Run `hubzoid eval run --required` on the earlier commit to confirm it still
passes before it goes live ([CI](ci.md)).

## Roll back the Hubzoid version

An upgrade can change the operational store's schema, and an earlier version
refuses a schema it does not know. So going back is always restore, then
install.

1. **Before every upgrade**, take a backup and check it:

   ```bash
   hubzoid backup ./my-hub --out pre-upgrade.tar.gz
   hubzoid backup verify pre-upgrade.tar.gz
   ```

   A backup holds the deployment's state. The hub's own files (`AGENTS.md`,
   skills, knowledge) belong in Git and are not in it. `.env` files and
   signing keys are included only with `--include-secrets` ([Backup](BACKUP.md)).
2. **To go back**, stop the hub or gateway, then:

   ```bash
   hubzoid restore pre-upgrade.tar.gz
   pip install "hubzoid==<previous version>"
   ```

   With PostgreSQL, add `--postgres` so the database goes back too.
   `--postgres-env` names its variable when it is not `DATABASE_URL`. A plain
   restore puts back the files only, and the earlier version refuses the newer
   schema ([PostgreSQL](BACKUP.md#postgresql)).

   With an image, run the previous image tag or digest on the restored data.
3. Start it and run `hubzoid doctor` and `hubzoid eval run --required`.

Anything people did after the upgrade (new conversations, approvals, runs) is
in the newer store, not the backup. Check the release's
[Upgrading](UPGRADING.md) section for steps that version needs on the way back.

Settings a newer version added are ignored by an older one. A hub that sets
`guardrails:` in `AGENTS.md` runs without guardrails on a version before 2.0.9
([Guardrails](guardrails.md)).

## Work in progress across a switch

Workflow runs belong to the code that started them. That code is the Hubzoid
version plus the hub's `workflows/**/*.py`, so rolling back either one counts
as a switch.

| Run | At the next start |
|---|---|
| Interrupted or queued under the old code | Cancelled. It shows as `CANCELLED` |
| A markdown task or scheduled eval suite that was queued but not started | Queued again under the current code |
| A work item | Stops for a person to restart it from a saved stage ([Work items](work-items.md)) |

A backup waits for queued and running work to finish before it is taken. A run
caught mid-way runs again at its next slot after a restore
([Backup](BACKUP.md#what-happens-to-running-work), [Workflows](workflows.md)).

## Rehearse a rollback

Before an upgrade, or once a quarter:

1. `hubzoid restore pre-upgrade.tar.gz --rehearse` restores into a scratch
   folder, upgrades a copy and reports what came back, without touching the
   deployment ([Rehearsing a restore](BACKUP.md#rehearsing-a-restore)).
2. On a spare machine, restore the backup, install the previous version, start
   it and run `hubzoid eval run --required`.

## Troubleshooting

**The earlier version refuses to start.** The store was upgraded by a newer
Hubzoid. Restore the backup taken before the upgrade, with `--postgres` when
the hub uses PostgreSQL.

**`hubzoid reload` changes nothing.** Reload checks the hub's folder first. A
folder with problems is not reloaded. Run `hubzoid doctor ./my-hub` to see why.
