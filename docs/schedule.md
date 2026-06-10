# Scheduled tasks — `<hub>/schedule/*.md`

A hub can declare background jobs the same way it declares skills and
knowledge: **one markdown file per job**. Frontmatter says *when* it runs and
*what the run may commit*; the body is plain-English instructions that the
hub's own agent executes unattended.

Hubzoid owns the mechanism — discovery, cron timing, the bounded run harness,
logging, scoped git capture. The hub author owns the policy — the schedule,
the instructions, the paths. There is no fixed built-in job: an IRS hub may
keep `knowledge/` in step with code commits, an Odoo hub may document each
upgrade per repo, another hub may do something entirely different.

Tasks fire **inside `hubzoid run`** (the bridge process). Deploying the hub
is deploying its schedule — no extra systemd timers or crontabs.

```
schedule/knowledge-refresh.md     <- you write this
        │  (cron match, hub idle)
        ▼
hubzoid scheduler (in-process)    <- tick every 30s, one run at a time
        │
        ▼
run harness                        <- fresh-context rounds until STATUS: DONE
  agent = the hub's own Runtime    <- claude-local OR any OpenAI/LiteLLM model
  tools = run_git, write_hub_file  <- injected for the run only, path-guarded
        │
        ▼
scoped git commit (+ push)         <- only the declared paths, never the rest
```

## A task file

`<hub>/schedule/knowledge-refresh.md`:

```markdown
---
schedule: "7 3 * * 1"        # 5-field cron, local server time (Mon 03:07)
timeout: 1800                 # seconds per round (default 1800)
max_rounds: 10                # fresh-context rounds per run (default 10)
max_turns: 40                 # agent turns within one round (default 40)
commit: ["knowledge/"]        # hub paths Hubzoid commits after DONE (optional)
push: true                    # pull --rebase + push after the commit (optional)
# write: ["knowledge/"]       # writable but NOT auto-committed — use instead of
                              # commit: while testing, then review the diff by hand
enabled: true                 # default true
---

Keep the docs in knowledge/ in step with the source repos under raw_data/.

- Pull every git repo under raw_data/ (run_git "pull").
- Your state file records the last-processed commit SHA per repo. Process
  everything from that SHA to HEAD. On the very first run (no SHA recorded),
  only look at the last 7 days of commits.
- For each new commit, read its diff (run_git "show <sha>") and update the
  relevant knowledge/*.md so they describe the CURRENT state of the system.
  Never write changelog-style "was X, now Y" sentences. Preserve each doc's
  frontmatter.
- A commit with no user-visible effect needs no doc change — just record it.
- After processing each repo, write its new SHA to your state file.
- You are done when every repo's recorded SHA equals its HEAD.
```

The filename stem (`knowledge-refresh`) is the task name. Frontmatter is
YAML; only `schedule:` is required. Cron is numeric 5-field (minute, hour,
day-of-month, month, day-of-week with 0=Sunday), evaluated in the server's
local time. Tip: pick an off-minute (`7 3` not `0 3`) — it makes log
correlation easier and avoids colliding with other on-the-hour jobs.

## How a run works (the harness contract)

When a task fires, Hubzoid runs the hub's own agent — same `AGENTS.md`
persona, same skills and read tools, same `MODEL` from `.env`. This works
identically on `claude-local` and on any OpenAI/LiteLLM model.

Each run is a loop of **rounds**. A round is one fresh-context agent session
fed: a harness preamble (operating rules below) + your task body + a carry
note from the previous round. Fresh contexts are what make big backlogs safe
— a 300-commit week is chunked across rounds automatically instead of
overflowing one session.

The agent must end every round with exactly one line:

```
STATUS: DONE — <one-line summary>        (the goal is fully met)
STATUS: CONTINUE — <what remains>        (anything is left)
```

That line is the **only** completion signal: the runner string-matches it.
`DONE` ends the run. `CONTINUE` (or a missing line) starts the next round.
Three hard caps bound every run — `timeout` seconds per round, `max_rounds`
rounds, `max_turns` agent turns per round — so a confused agent ends as
`incomplete`, never hangs.

**State file.** The preamble orders the agent to keep its progress in
`.hubzoid/schedule/<task>/state.json` and update it after every unit of
work. That file is the only continuity between rounds *and between runs*: a
run that ends incomplete (or a server that reboots mid-run) loses nothing —
the next fire resumes from the recorded state. Write your task body around
it ("your state file records X").

**Tools available during a run** (and only during a run — chat never sees
these):

| Tool | What it allows |
|---|---|
| `run_git(repo, args)` | Read/sync git on checkouts inside the hub: `pull`, `fetch`, `log`, `diff`, `show`, `status`, `rev-parse`, `ls-files`, `branch`, `shortlog`, `describe`, read-only `remote`. Mutating verbs are refused. |
| `write_hub_file(path, content)` | Create/overwrite a file, but only under the task's `write:` + `commit:` paths + its own scratch dir. `.env`, secrets, `raw_data/` clones, `AGENTS.md` are physically unreachable. |
| the usual read tools | `read_file`, `list_files`, `grep_data`, knowledge/skill tools — whatever the hub already has. |

Need something deterministic or bespoke (call an API, run a build)? Add a
normal `tools_local/*.py` tool to the hub and mention it in the task body —
no platform change needed.

## Commit and push — done by Hubzoid, not the agent

After a `DONE` round, if the task declares `commit:` paths, Hubzoid stages
and commits **exactly those pathspecs** — message
`schedule(<task>): <agent's one-line summary>` — and with `push: true` does
`git pull --rebase` then `git push`. A rebase conflict aborts cleanly: the
commit stays local, the run is recorded as `error`, a human resolves.

The scoping is the safety property for unattended servers: a dirty tree
elsewhere (source clones in `raw_data/`, a stray `.webui_secret_key`, local
edits) is **never** swept into the commit. The agent itself cannot commit or
push at all.

Prereqs on the box, once: the hub's repo has a remote + tracking branch,
`git config user.name/email` set, and credentials that work non-interactively
(credential store or SSH deploy key).

## Timing model

* The scheduler ticks every 30 s inside the bridge and re-reads
  `schedule/*.md` each tick — edits apply live, no restart.
* **Idle gate**: a due task won't start while a chat request is in flight;
  it fires on a later tick.
* **Catch-up**: next-fire is computed from the task's last fire (or first
  discovery). If the server was down over Monday 03:07, the task fires once
  on the first tick after boot — once, not once per missed week.
* A brand-new task file anchors at discovery: it first fires at its next
  *future* cron match (use `hubzoid schedule run` to test immediately).
* One run at a time per hub, enforced with a lock file across processes.
* Kill switch: `HUBZOID_DISABLE_SCHEDULE=1` in the environment.

## Observability

Every run appends JSONL to
`.hubzoid/schedule/<task>/runs/<timestamp>.jsonl` — run/round boundaries,
every `run_git`/`write_hub_file` call, the agent's full reply per round, the
parsed STATUS, commit/push results, errors with tracebacks. Flushed per
event, so `tail -f` follows a live run. The scheduler also mirrors the
important lines to the process log, and
`.hubzoid/schedule-state.json` records `last_fired`/`last_result` per task.

```bash
hubzoid schedule list <hub>      # tasks, cadence, next fire, last result
hubzoid schedule status <hub>    # recorded history per task
tail -f <hub>/.hubzoid/schedule/<task>/runs/*.jsonl
```

Add `.hubzoid/` to the hub's `.gitignore` (the `hubzoid init` template
already has it) — it's runtime state, not content.

## Testing a task (do this before trusting the cron)

```bash
# 1. No-LLM checks: frontmatter + cron parse, and the exact prompt the agent gets
hubzoid doctor <hub>
hubzoid schedule run <hub> <task> --dry-run

# 2. Real end-to-end run, bounded tight while you iterate on the wording.
#    While testing, declare `write:` instead of `commit:` so the run edits
#    the files but leaves the diff for you to review by hand.
hubzoid schedule run <hub> <task> --timeout 300 --max-rounds 2

# 3. Inspect what it did
tail -f <hub>/.hubzoid/schedule/<task>/runs/<ts>.jsonl   # during
git -C <hub> diff                                         # after (drop commit: while testing)
```

`schedule run` uses the same harness, model and lock as a scheduled fire —
if it works manually, the cron fire is the same thing on a timer. Exit code
0 means the agent reported DONE.

## Production

Nothing extra. The same systemd service (or container) that runs
`hubzoid run <hub>` runs the schedule. New knowledge committed by a task is
picked up by chat on the hub's next restart.

**Migrating from `hubzoid knowledge refresh` (≤0.5.x):** that subsystem
(`knowledge plan/pending/mark-done/status/refresh`, the `.knowledge-sync/`
cursor, the separate weekly timer unit) is gone. Replace it with a
`schedule/knowledge-refresh.md` like the example above, delete
`<hub>/.knowledge-sync/` and the timer unit, and let `hubzoid run` do the
rest. The example body reproduces the old behavior — including the
7-day first-run window and the current-state-not-changelog rule — and is
now fully editable per hub, e.g. make it write per-repo upgrade notes
instead.
