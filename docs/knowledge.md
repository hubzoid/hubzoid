# Refreshing knowledge from source code

A hub's `knowledge/*.md` is the curated, high-level understanding the agent
reads to orient itself; `raw_data/` is the raw source it greps for specifics.
As the code changes, the knowledge docs drift. `hubzoid knowledge` keeps them
in step — **deterministic prep + an agentic /goal worker**, split on purpose:

- **Prep (deterministic):** pull each source repo, enumerate the commits since
  the last successful refresh, and track a per-commit worklist + a per-repo
  SHA cursor on disk under `<hub>/.knowledge-sync/`. No LLM — so it's
  exhaustive and resumable.
- **Worker (agentic):** a headless `claude -p` session loads the
  [`update-knowledge`](../.claude/skills/update-knowledge/SKILL.md) procedure,
  reads the pending worklist, updates the affected docs, and marks each commit
  done — kept running by `/goal` until nothing is pending.

The cursor advances **only when the whole worklist is clear**, so a refresh is
all-or-nothing per run and safe to re-run.

## Where the source repos come from

Every git checkout directly under `<hub>/raw_data/` is a source. Override with
`<hub>/.knowledge-sync/repos.json`:

```json
{ "repos": { "core": "raw_data/core", "api": "/abs/path/to/api" } }
```

## Commands

```bash
hubzoid knowledge plan <hub>      # pull + show how many commits are pending
hubzoid knowledge refresh <hub>   # the full loop: prep → /goal worker(s) → advance cursor
hubzoid knowledge status <hub>    # cursor + pending count
hubzoid knowledge pending <hub>   # list pending commits (used by the worker)
hubzoid knowledge mark-done <hub> <sha>...
```

`refresh` runs **fresh** `claude -p` /goal sessions in a loop until the
worklist is clear. Each session gets a clean context, so a single one hitting
a context limit never drops commits — the next session resumes from the
remaining pending commits. It stops early (without advancing the cursor) if a
round makes no progress, so it can't loop forever.

After a successful refresh: **review the diff**, commit the knowledge changes,
and restart the hub so the new knowledge loads (`raw_data/` is read live and
needs no restart; `knowledge/` is loaded at process start).

## Triggering

- **Manual (start here):** run `hubzoid knowledge refresh <hub>` yourself, or
  invoke the `update-knowledge` skill interactively in `claude` and review as
  you go. Best while you're still building trust in the output.
- **Scheduled:** a cron job or systemd timer running the same command
  (nightly incremental; a periodic full pass after clearing the cursor).
- **On demand / orchestrated:** wrap the command behind a small
  bearer-authenticated trigger, or drive it from your own job platform — the
  command is the unit of work, the trigger is yours to choose. Keep any
  trigger endpoint on loopback or strongly authenticated; it runs an agent
  with write access to the hub.

`MODEL=claude-local` (the subscription token, see [DEPLOYING.md §5b](DEPLOYING.md))
is the worker's auth — the same engine the hubs run, no per-token API billing.
