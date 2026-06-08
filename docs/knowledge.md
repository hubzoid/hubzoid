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

## The first refresh

There's no cursor yet on a repo's first refresh (or when you add a new repo to
`raw_data/`), so without a bound it would try to fold the repo's **entire
history** into knowledge. Instead the first run is windowed: `--since-days`
(default **7**) takes only commits from the last N days. The assumption is that
`knowledge/*.md` already reflects the current code, so you just want to start
tracking changes from roughly now. Widen it for a one-off catch-up:

```bash
hubzoid knowledge refresh <hub> --since-days 30   # first run: fold in the last month
```

Once a repo has a cursor, `--since-days` no longer applies to it — subsequent
runs always enumerate `cursor..HEAD`. (Want a clean full re-derive later? Delete
`<hub>/.knowledge-sync/state.json` and run with a large `--since-days`.)

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

By default `refresh` leaves the updated `knowledge/*.md` in the working tree
for you to **review and commit by hand** — the right mode while you're building
trust. Two flags make it self-contained for unattended/scheduled runs:

```bash
hubzoid knowledge refresh <hub> --commit   # git-commit ONLY the knowledge/ path on success
hubzoid knowledge refresh <hub> --push     # implies --commit, then pull --rebase + push
```

`--commit` keeps the working tree clean so the next code deploy (`git pull`)
isn't blocked by a dirty tree. It commits **only** the `knowledge/` pathspec, so
a dirty `raw_data/` or unrelated local edits are never swept in. `--push`
integrates the remote first (`pull --rebase`, so the knowledge commit replays on
top of any code commits pushed meanwhile) and pushes; a rebase conflict is
aborted cleanly and the commit kept local for a human to resolve.

After a successful refresh, **restart the hub** so the new knowledge loads
(`raw_data/` is read live and needs no restart; `knowledge/` is loaded at
process start). Restart is a deploy concern, deliberately **not** done by the
command — wire it into your scheduler (e.g. systemd `ExecStartPost`).

## Triggering

- **Manual (start here):** run `hubzoid knowledge refresh <hub>` yourself, or
  invoke the `update-knowledge` skill interactively in `claude` and review as
  you go. Best while you're still building trust in the output.
- **Scheduled on prod:** a weekly systemd timer running
  `refresh --commit` (or `--push`) directly on the production box — see
  [Weekly refresh on prod](#weekly-refresh-on-prod) below.
- **On demand / orchestrated:** wrap the command behind a small
  bearer-authenticated trigger, or drive it from your own job platform — the
  command is the unit of work, the trigger is yours to choose. Keep any
  trigger endpoint on loopback or strongly authenticated; it runs an agent
  with write access to the hub.

`MODEL=claude-local` (the subscription token, see [DEPLOYING.md §5b](DEPLOYING.md))
is the worker's auth — the same engine the hubs run, no per-token API billing.

## Weekly refresh on prod

Run the weekly refresh **on the production box itself** — the box that already
runs the hub. The prereqs and the three roles (source repos / refresh runner /
prod) are the same; here the runner *is* prod, so prod also holds the source
checkouts. The flow:

```
raw_data/ source clones ──► refresh --commit ──► knowledge/ commit ──► restart hub
 (on prod, on uat/dev)        (on prod, weekly)    (working tree clean)  (loads new knowledge)
```

### Prereqs on prod (one-time)

1. **Clone the source repos into `<hub>/raw_data/`**, each checked out on the
   branch you want tracked (`git clone -b uat …`, `-b dev …`). The refresh
   follows whatever branch each repo has checked out — different repos can be on
   different branches.
2. **Gitignore those clones** so they never enter the agents repo. In
   `<hub>/.gitignore`:
   ```gitignore
   raw_data/<repo-a>/
   raw_data/<repo-b>/
   .knowledge-sync/        # cursor/worklist = runtime state, keep out of git
   ```
   (Static reference material you *do* want shipped via git — PDFs, exports —
   can still live in `raw_data/` and be committed; only the source clones are
   ignored.)
3. **Set a git committer identity** for the auto-commit, once, in the agents repo:
   ```bash
   git -C /opt/hubzoid/agents config user.name  hubzoid-prod
   git -C /opt/hubzoid/agents config user.email hubzoid@your-org.com
   ```

### The timer

The whole cycle is one command, so no wrapper script — `ExecStart` is the
refresh, `ExecStartPost` restarts the hub to load the result.

`/etc/systemd/system/hubzoid-knowledge.service`:

```ini
[Unit]
Description=Weekly Hubzoid knowledge refresh (IRS)
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=hubzoid
Environment=MODEL=claude-local
# The worker shells out to `claude`; systemd's default PATH omits ~/.local/bin.
Environment=PATH=/opt/hubzoid/.local/bin:/usr/local/bin:/usr/bin:/bin
TimeoutStartSec=infinity          # LLM sessions can run a while
ExecStart=/opt/hubzoid/agents/.venv/bin/hubzoid knowledge refresh /opt/hubzoid/agents/irs-hub --commit
# Restart only the hub unit so it picks up the new knowledge/ (loaded at start).
ExecStartPost=/usr/bin/systemctl restart hubzoid@irs-hub
```

`/etc/systemd/system/hubzoid-knowledge.timer`:

```ini
[Unit]
Description=Weekly Hubzoid knowledge refresh (IRS)

[Timer]
OnCalendar=Sun *-*-* 03:00:00     # off-peak; the restart causes a brief blip
Persistent=true                   # catch up if the box was off at the scheduled time

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl enable --now hubzoid-knowledge.timer
systemctl list-timers hubzoid-knowledge.timer    # confirm next run
journalctl -u hubzoid-knowledge.service -f       # watch a run
```

### The restart needs privilege

The job runs as `hubzoid`, but `systemctl restart` of a system unit needs root.
Grant just that one command via sudoers (`visudo -f /etc/sudoers.d/hubzoid`),
and change the `ExecStartPost` line to `… /usr/bin/sudo /usr/bin/systemctl …`:

```
hubzoid ALL=(root) NOPASSWD: /usr/bin/systemctl restart hubzoid@irs-hub
```

### Push or not?

- **`--commit` only (simplest):** prod is self-contained; knowledge commits live
  only on prod. No deploy key needed. The history is regenerable from source, but
  fold `knowledge/` into your backup if you want it preserved.
- **`--push` (if you have other environments/CI):** prod commits *and* pushes to
  the agents repo so the remote stays current and any clone gets the latest
  knowledge. Needs a write deploy key. Because prod is the sole writer of
  `knowledge/` and code lands on different files, the built-in `pull --rebase`
  keeps the push clean.

### Things you accept by refreshing on prod

- **No human review** — the worker edits `knowledge/` unattended and the agent
  serves it after restart. It's committed, so a bad refresh is one `git revert`
  away. Want a gate instead? Drop `--push`/restart and have the job open a PR.
- **A restart blip** — schedule it off-peak (Sun 3am above).
- **Source code on prod** — the clones live under `raw_data/`; keep them
  gitignored so they don't leak into the agents repo.
