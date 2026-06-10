"""Tests for hub-owned scheduled tasks (<hub>/schedule/*.md).

Covers the three layers separately and end-to-end with a stubbed Runtime:

  * scheduling.py  — cron parsing/next-fire, the task loader + validation,
                     fire-state anchors (incl. missed-run catch-up), run lock
  * schedule_runner.py — the round harness (DONE / CONTINUE / no-STATUS /
                     timeout / backend-error paths), JSONL logging, and the
                     scoped commit+push capture against real git repos
  * tools/schedule_tools.py — run_git whitelist + write_hub_file path guard
  * scheduler.py   — due computation, idle gate, lock skip, disable switch
  * cli.py         — schedule list / run / status

A real LLM end-to-end lives in tests/e2e/test_schedule_e2e.py (marked e2e).
"""
from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from agents.tool_context import ToolContext
from typer.testing import CliRunner

from hubzoid import cli
from hubzoid import schedule_runner as runner
from hubzoid import scheduler as scheduler_lib
from hubzoid import scheduling as sch
from hubzoid.tools import schedule_tools


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _run(args, cwd):
    subprocess.run(args, cwd=str(cwd), check=True, capture_output=True, text=True)


def _git_repo(path: Path, subjects: list[str]) -> list[str]:
    path.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-q"], path)
    _run(["git", "config", "user.email", "t@t.dev"], path)
    _run(["git", "config", "user.name", "tester"], path)
    shas = []
    for i, subj in enumerate(subjects):
        (path / f"f{i}.txt").write_text(str(i))
        _run(["git", "add", "-A"], path)
        _run(["git", "commit", "-q", "-m", subj], path)
        shas.append(
            subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                           capture_output=True, text=True, check=True).stdout.strip()
        )
    return shas


def _task(name="t", cron="0 3 * * *", body="do the thing", **kw) -> sch.ScheduledTask:
    return sch.ScheduledTask(
        name=name, schedule=cron, cron=sch.parse_cron(cron), body=body, **kw,
    )


def _invoke(tool, **kwargs) -> str:
    """Call a FunctionTool through the SDK's invocation path."""
    args = json.dumps(kwargs)
    ctx = ToolContext(
        context=None, tool_name=tool.name,
        tool_call_id="test", tool_arguments=args,
    )
    return asyncio.run(tool.on_invoke_tool(ctx, args))


class StubRuntime:
    """Scripted Runtime: returns canned replies, records every prompt."""

    name = "stub"

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts: list[str] = []

    async def run(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.replies.pop(0) if self.replies else "STATUS: CONTINUE — out of script"


def _factory_for(rt):
    def factory(hub_dir, task, emit):
        return rt
    return factory


# ===========================================================================
# cron
# ===========================================================================
def test_cron_parse_basics():
    c = sch.parse_cron("7 3 * * 1")
    assert c.minutes == {7} and c.hours == {3}
    assert c.dom_star and not c.dow_star and c.dows == {1}
    c2 = sch.parse_cron("*/15 0,12 1-3 * *")
    assert c2.minutes == {0, 15, 30, 45} and c2.hours == {0, 12} and c2.doms == {1, 2, 3}


def test_cron_dow_seven_is_sunday():
    assert sch.parse_cron("0 0 * * 7").dows == {0}


@pytest.mark.parametrize("bad", [
    "* * * *",            # 4 fields
    "60 * * * *",         # minute out of range
    "* 24 * * *",         # hour out of range
    "* * 0 * *",          # dom out of range
    "* * * 13 *",         # month out of range
    "* * * * 8",          # dow out of range
    "*/0 * * * *",        # zero step
    "a * * * *",          # junk
    "5-1 * * * *",        # inverted range
])
def test_cron_rejects_garbage(bad):
    with pytest.raises(ValueError):
        sch.parse_cron(bad)


def test_next_fire_every_five_minutes():
    c = sch.parse_cron("*/5 * * * *")
    nxt = sch.next_fire(c, datetime(2026, 6, 8, 10, 2))
    assert nxt == datetime(2026, 6, 8, 10, 5)


def test_next_fire_weekly_monday():
    c = sch.parse_cron("7 3 * * 1")
    # 2026-06-08 is a Monday. Before 03:07 -> same day; at 03:07 -> next week.
    assert sch.next_fire(c, datetime(2026, 6, 8, 3, 0)) == datetime(2026, 6, 8, 3, 7)
    assert sch.next_fire(c, datetime(2026, 6, 8, 3, 7)) == datetime(2026, 6, 15, 3, 7)


def test_next_fire_dom_dow_or_rule():
    # Standard cron quirk: both restricted => EITHER matches.
    c = sch.parse_cron("0 0 13 * 5")
    # From Tue 2026-06-09: Friday the 12th comes before the 13th.
    assert sch.next_fire(c, datetime(2026, 6, 9)) == datetime(2026, 6, 12, 0, 0)
    assert sch.next_fire(c, datetime(2026, 6, 12, 0, 0)) == datetime(2026, 6, 13, 0, 0)


def test_next_fire_unsatisfiable_returns_none():
    assert sch.next_fire(sch.parse_cron("0 0 30 2 *"), datetime(2026, 1, 1)) is None


# ===========================================================================
# loader
# ===========================================================================
def _write_task(hub: Path, name: str, fm: str, body: str = "Do the work.") -> Path:
    sdir = hub / "schedule"
    sdir.mkdir(parents=True, exist_ok=True)
    p = sdir / f"{name}.md"
    p.write_text(f"---\n{fm}\n---\n\n{body}\n")
    return p


def test_loader_discovers_and_defaults(tmp_path):
    _write_task(tmp_path, "refresh", 'schedule: "7 3 * * 1"')
    tasks, problems = sch.load_tasks(tmp_path)
    assert problems == []
    [t] = tasks
    assert t.name == "refresh"
    assert t.timeout == sch.DEFAULT_TIMEOUT
    assert t.max_rounds == sch.DEFAULT_MAX_ROUNDS
    assert t.max_turns == sch.DEFAULT_MAX_TURNS
    assert t.commit == [] and t.push is False and t.enabled
    assert "Do the work." in t.body
    assert t.scratch_rel == ".hubzoid/schedule/refresh"


def test_loader_full_frontmatter_and_disabled(tmp_path):
    _write_task(tmp_path, "full",
                'schedule: "0 4 * * *"\ntimeout: 600\nmax_rounds: 3\n'
                'max_turns: 25\ncommit: ["knowledge/"]\npush: true\nenabled: false')
    [t], problems = sch.load_tasks(tmp_path)
    assert problems == []
    assert (t.timeout, t.max_rounds, t.max_turns) == (600, 3, 25)
    assert t.commit == ["knowledge/".rstrip("/")] or t.commit == ["knowledge"]
    assert t.push is True and t.enabled is False
    assert t.writable_paths() == ["knowledge", ".hubzoid/schedule/full"]


def test_loader_commit_accepts_single_string(tmp_path):
    _write_task(tmp_path, "one", 'schedule: "0 4 * * *"\ncommit: knowledge/')
    [t], _ = sch.load_tasks(tmp_path)
    assert t.commit == ["knowledge"]


def test_loader_write_grants_writable_without_commit(tmp_path):
    """`write:` = writable but NOT auto-committed (review-by-hand mode).
    Overlap with commit: is deduped in writable_paths()."""
    _write_task(tmp_path, "rw", 'schedule: "0 4 * * *"\nwrite: ["knowledge/"]')
    [t], problems = sch.load_tasks(tmp_path)
    assert problems == []
    assert t.write == ["knowledge"] and t.commit == []
    assert t.writable_paths() == ["knowledge", ".hubzoid/schedule/rw"]
    _write_task(tmp_path, "both",
                'schedule: "0 4 * * *"\nwrite: ["knowledge/"]\ncommit: ["knowledge/"]')
    tasks, _ = sch.load_tasks(tmp_path)
    t2 = next(t for t in tasks if t.name == "both")
    assert t2.writable_paths() == ["knowledge", ".hubzoid/schedule/both"]


@pytest.mark.parametrize("fm,needle", [
    ("timeout: 600", "schedule"),                                  # missing schedule
    ('schedule: "61 * * * *"', "range"),                           # bad cron
    ('schedule: "0 0 30 2 *"', "never matches"),                   # Feb 30
    ('schedule: "0 4 * * *"\ncommit: ["../escape"]', ".."),        # path escape
    ('schedule: "0 4 * * *"\npush: true', "push"),                 # push w/o commit
    ('schedule: "0 4 * * *"\ntimeout: 0', "timeout"),              # bad int
])
def test_loader_rejects_bad_tasks_as_problems(tmp_path, fm, needle):
    _write_task(tmp_path, "bad", fm)
    tasks, problems = sch.load_tasks(tmp_path)
    assert tasks == []
    assert len(problems) == 1 and needle in problems[0]


def test_loader_empty_body_rejected(tmp_path):
    _write_task(tmp_path, "empty", 'schedule: "0 4 * * *"', body="")
    tasks, problems = sch.load_tasks(tmp_path)
    assert tasks == [] and "empty" in problems[0]


def test_loader_one_bad_task_does_not_hide_good_one(tmp_path):
    _write_task(tmp_path, "good", 'schedule: "0 4 * * *"')
    _write_task(tmp_path, "bad", "timeout: 5")
    tasks, problems = sch.load_tasks(tmp_path)
    assert [t.name for t in tasks] == ["good"] and len(problems) == 1


def test_loader_skips_readme_and_accepts_alias_dir(tmp_path):
    sdir = tmp_path / "Schedules"          # case+plural alias
    sdir.mkdir()
    (sdir / "README.md").write_text("docs, not a task")
    (sdir / "job.md").write_text('---\nschedule: "0 4 * * *"\n---\n\nwork\n')
    tasks, problems = sch.load_tasks(tmp_path)
    assert [t.name for t in tasks] == ["job"] and problems == []


# ===========================================================================
# fire-state: anchors, catch-up
# ===========================================================================
def test_new_task_anchors_now_not_retroactively(tmp_path):
    t = _task(cron="0 3 * * *")
    state = sch.ScheduleState(tmp_path)
    now = datetime(2026, 6, 9, 12, 0)
    assert not sch.is_due(t, state, now)                      # first sight: not due
    nxt = sch.next_fire_for(t, state, now)
    assert nxt == datetime(2026, 6, 10, 3, 0)                  # next future match
    assert state.get(t.name).get("first_seen_at")              # anchor persisted


def test_due_after_cron_match_passes_and_clears_on_fire(tmp_path):
    t = _task(cron="0 3 * * *")
    state = sch.ScheduleState(tmp_path)
    state.record_seen(t.name, datetime(2026, 6, 9, 12, 0))
    assert sch.is_due(t, state, datetime(2026, 6, 10, 3, 1))   # match passed -> due
    state.record_fired(t.name, datetime(2026, 6, 10, 3, 1), result="done")
    assert not sch.is_due(t, state, datetime(2026, 6, 10, 3, 5))  # fired -> not due
    assert sch.is_due(t, state, datetime(2026, 6, 11, 3, 1))      # due again next day


def test_missed_runs_collapse_to_one_catch_up_fire(tmp_path):
    """Down for 3 weeks across a weekly cron => due once, not three times."""
    t = _task(cron="7 3 * * 1")
    state = sch.ScheduleState(tmp_path)
    state.record_fired(t.name, datetime(2026, 5, 18, 3, 7), result="done")
    now = datetime(2026, 6, 9, 9, 0)                           # 3 Mondays missed
    assert sch.is_due(t, state, now)
    state.record_fired(t.name, now, result="done")             # one catch-up fire
    assert not sch.is_due(t, state, datetime(2026, 6, 9, 9, 5))


def test_state_survives_corrupt_file(tmp_path):
    state = sch.ScheduleState(tmp_path)
    state.path.parent.mkdir(parents=True, exist_ok=True)
    state.path.write_text("{not json")
    assert state.get("x") == {}                                # no crash
    state.record_seen("x", datetime.now())                     # rewrites cleanly
    assert state.get("x").get("first_seen_at")


# ===========================================================================
# run lock
# ===========================================================================
def test_runlock_excludes_and_releases(tmp_path):
    a, b = sch.RunLock(tmp_path), sch.RunLock(tmp_path)
    assert a.acquire("t1")
    assert not b.acquire("t2")                                 # held by live pid
    a.release()
    assert b.acquire("t2")
    b.release()


def test_runlock_steals_stale_dead_pid(tmp_path):
    lock_path = tmp_path / sch.STATE_DIRNAME / sch.LOCK_FILENAME
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text(json.dumps({"pid": 99999999, "task": "ghost"}))
    assert sch.RunLock(tmp_path).acquire("t")                  # dead holder -> steal


# ===========================================================================
# runner: the round harness
# ===========================================================================
def test_parse_status_variants():
    assert runner.parse_status("blah\nSTATUS: DONE — all synced") == ("done", "all synced")
    assert runner.parse_status("status: continue - 3 repos left") == ("continue", "3 repos left")
    assert runner.parse_status("no signal here") == (None, "")
    two = "STATUS: CONTINUE — more\nwork\nSTATUS: DONE — finished"
    assert runner.parse_status(two) == ("done", "finished")    # last one wins


def test_prompt_contains_the_contract(tmp_path):
    t = _task(name="refresh", body="Sync the docs.", commit=["knowledge"])
    p = runner.build_prompt(t, tmp_path, round_no=2, carry="CONTINUE — repo b left")
    assert "STATUS: DONE" in p and "STATUS: CONTINUE" in p     # finish protocol
    assert ".hubzoid/schedule/refresh/state.json" in p         # state file path
    assert "knowledge/" in p                                   # writable paths
    assert "Sync the docs." in p                               # the hub's body
    assert "repo b left" in p                                  # carry threaded in
    assert "round 2/" in p


def test_run_done_first_round(tmp_path):
    t = _task(name="quick")
    rt = StubRuntime(["did everything\nSTATUS: DONE — all updated"])
    res = asyncio.run(runner.run_task(tmp_path, t, runtime_factory=_factory_for(rt)))
    assert res.ok and res.result == "done"
    assert res.rounds == 1 and res.summary == "all updated"
    # state recorded
    assert sch.ScheduleState(tmp_path).get("quick")["last_result"] == "done"
    # JSONL log written with start/end events
    events = [json.loads(l) for l in res.run_log.read_text().splitlines()]
    kinds = [e["event"] for e in events]
    assert kinds[0] == "run_start" and kinds[-1] == "run_end"
    assert "agent_reply" in kinds and "round_end" in kinds


def test_run_continue_then_done_carries_note(tmp_path):
    t = _task(name="two")
    rt = StubRuntime([
        "half done\nSTATUS: CONTINUE — repo core remains",
        "rest done\nSTATUS: DONE — both repos folded",
    ])
    res = asyncio.run(runner.run_task(tmp_path, t, runtime_factory=_factory_for(rt)))
    assert res.ok and res.rounds == 2
    assert "repo core remains" in rt.prompts[1]                # carry reached round 2


def test_run_no_status_hits_round_cap_incomplete(tmp_path):
    t = _task(name="lost", max_rounds=3)
    rt = StubRuntime(["did stuff, forgot the protocol"] * 5)
    res = asyncio.run(runner.run_task(tmp_path, t, runtime_factory=_factory_for(rt)))
    assert res.result == "incomplete" and res.rounds == 3
    assert sch.ScheduleState(tmp_path).get("lost")["last_result"] == "incomplete"


def test_run_round_timeout_is_bounded_and_logged(tmp_path):
    t = _task(name="slow", max_rounds=2)
    t.timeout = 0.05                                            # tiny for the test

    class Sleeper:
        async def run(self, prompt):
            await asyncio.sleep(1)
            return "STATUS: DONE — too late"

    res = asyncio.run(runner.run_task(tmp_path, t, runtime_factory=_factory_for(Sleeper())))
    assert res.result == "incomplete" and res.rounds == 2
    kinds = [json.loads(l)["event"] for l in res.run_log.read_text().splitlines()]
    assert kinds.count("round_timeout") == 2


def test_run_aborts_after_consecutive_backend_errors(tmp_path):
    t = _task(name="dead", max_rounds=10)
    rt = StubRuntime(["\n\n[agent error: AuthError: no credit]"] * 10)
    res = asyncio.run(runner.run_task(tmp_path, t, runtime_factory=_factory_for(rt)))
    assert res.result == "error" and res.rounds == 3            # bailed early
    assert "backend erroring" in res.error


def test_run_survives_runtime_factory_crash(tmp_path):
    t = _task(name="boom")

    def bad_factory(hub_dir, task, emit):
        raise RuntimeError("MODEL not configured")

    res = asyncio.run(runner.run_task(tmp_path, t, runtime_factory=bad_factory))
    assert res.result == "error" and "MODEL not configured" in res.error
    assert sch.ScheduleState(tmp_path).get("boom")["last_result"] == "error"


# ===========================================================================
# runner: scoped commit + push capture
# ===========================================================================
def _hub_repo(tmp_path) -> Path:
    hub = tmp_path / "hub"
    (hub / "knowledge").mkdir(parents=True)
    (hub / "knowledge" / "about.md").write_text("orig\n")
    (hub / "AGENTS.md").write_text("root\n")
    (hub / ".gitignore").write_text("raw_data/\n.hubzoid/\n")
    _run(["git", "init", "-q"], hub)
    _run(["git", "config", "user.email", "t@t.dev"], hub)
    _run(["git", "config", "user.name", "tester"], hub)
    _run(["git", "add", "-A"], hub)
    _run(["git", "commit", "-q", "-m", "init"], hub)
    return hub


def test_done_run_commits_only_declared_paths(tmp_path):
    hub = _hub_repo(tmp_path)
    t = _task(name="refresh", commit=["knowledge"])

    class Worker:
        async def run(self, prompt):
            (hub / "knowledge" / "about.md").write_text("updated by task\n")
            (hub / "AGENTS.md").write_text("stray edit\n")     # must NOT be swept in
            return "STATUS: DONE — docs updated"

    res = asyncio.run(runner.run_task(hub, t, runtime_factory=_factory_for(Worker())))
    assert res.ok and res.commit_sha
    changed = subprocess.run(["git", "-C", str(hub), "show", "--name-only", "--format=", "HEAD"],
                             capture_output=True, text=True, check=True).stdout.split()
    assert "knowledge/about.md" in changed
    assert "AGENTS.md" not in changed
    msg = subprocess.run(["git", "-C", str(hub), "log", "-1", "--format=%s"],
                         capture_output=True, text=True, check=True).stdout.strip()
    assert msg == "schedule(refresh): docs updated"
    porc = subprocess.run(["git", "-C", str(hub), "status", "--porcelain"],
                          capture_output=True, text=True, check=True).stdout
    assert "AGENTS.md" in porc                                  # stray left uncommitted


def test_done_run_pushes_to_remote(tmp_path):
    # Bare origin + working clone: prove pull --rebase + push lands upstream.
    bare = tmp_path / "origin.git"
    bare.mkdir()
    _run(["git", "init", "-q", "--bare"], bare)
    seed = _hub_repo(tmp_path)
    _run(["git", "remote", "add", "origin", str(bare)], seed)
    _run(["git", "push", "-q", "-u", "origin", "HEAD"], seed)

    t = _task(name="sync", commit=["knowledge"], push=True)

    class Worker:
        async def run(self, prompt):
            (seed / "knowledge" / "about.md").write_text("pushed content\n")
            return "STATUS: DONE — pushed update"

    res = asyncio.run(runner.run_task(seed, t, runtime_factory=_factory_for(Worker())))
    assert res.ok and res.commit_sha
    remote_msg = subprocess.run(["git", "-C", str(bare), "log", "-1", "--format=%s"],
                                capture_output=True, text=True, check=True).stdout.strip()
    assert remote_msg == "schedule(sync): pushed update"        # it reached origin


def test_done_run_outside_git_repo_is_error_not_crash(tmp_path):
    hub = tmp_path / "hub"
    (hub / "knowledge").mkdir(parents=True)
    t = _task(name="orphan", commit=["knowledge"])

    class Worker:
        async def run(self, prompt):
            (hub / "knowledge" / "x.md").write_text("data\n")
            return "STATUS: DONE — wrote docs"

    res = asyncio.run(runner.run_task(hub, t, runtime_factory=_factory_for(Worker())))
    assert res.result == "error" and "not inside a git repository" in res.error


def test_done_run_with_no_changes_skips_commit(tmp_path):
    hub = _hub_repo(tmp_path)
    t = _task(name="noop", commit=["knowledge"])
    rt = StubRuntime(["nothing to change\nSTATUS: DONE — already current"])
    res = asyncio.run(runner.run_task(hub, t, runtime_factory=_factory_for(rt)))
    assert res.ok and res.commit_sha is None


# ===========================================================================
# schedule tools
# ===========================================================================
def _tool_env(tmp_path):
    hub = tmp_path / "hub"
    (hub / "knowledge").mkdir(parents=True)
    _git_repo(hub / "raw_data" / "core", ["add billing", "fix tax rounding"])
    task = _task(name="refresh", commit=["knowledge"])
    events = []
    git, write = schedule_tools.make(hub, task, lambda **kw: events.append(kw))
    return hub, git, write, events


def test_run_git_allows_reads_and_logs(tmp_path):
    hub, git, _w, events = _tool_env(tmp_path)
    out = _invoke(git, repo="raw_data/core", args="log --oneline -5")
    assert "add billing" in out and "fix tax rounding" in out
    assert events and events[0]["tool"] == "run_git"
    assert events[0]["exit"] == 0 and "add billing" in events[0]["result"]


def test_run_git_failure_is_loud_and_logged(tmp_path):
    """A failed git command (e.g. a blocked pull) must scream FAILED at the
    agent and land in the JSONL with its exit code — a silent pull failure
    once made the agent doc-sync against a stale checkout."""
    hub, git, _w, events = _tool_env(tmp_path)
    out = _invoke(git, repo="raw_data/core", args="show deadbeef123")
    assert out.startswith("[exit ") and "FAILED" in out
    ev = events[-1]
    assert ev["tool"] == "run_git" and ev["exit"] != 0
    assert "deadbeef123" in ev["args"]


def test_preamble_warns_about_tool_failures_and_state_regression(tmp_path):
    p = runner.build_prompt(_task(), tmp_path, round_no=1)
    assert "Tool failures are NOT success" in p
    assert "Never regress your state file" in p


def test_run_git_refuses_mutations_and_escapes(tmp_path):
    hub, git, _w, _e = _tool_env(tmp_path)
    assert "refused" in _invoke(git, repo="raw_data/core", args="push origin main")
    assert "refused" in _invoke(git, repo="raw_data/core", args="checkout -b evil")
    assert "refused" in _invoke(git, repo="raw_data/core", args="commit -m x")
    assert "refused" in _invoke(git, repo="../../outside", args="log")
    assert "refused" in _invoke(git, repo="raw_data/core",
                                args="log --output=/tmp/evil.txt")
    assert "refused" in _invoke(git, repo="raw_data/core",
                                args="fetch --upload-pack=/bin/sh")
    assert "not a git checkout" in _invoke(git, repo="knowledge", args="log")


def test_write_hub_file_scopes_to_writable_paths(tmp_path):
    hub, _g, write, events = _tool_env(tmp_path)
    ok = _invoke(write, path="knowledge/billing.md", content="# Billing\n")
    assert "wrote" in ok
    assert (hub / "knowledge" / "billing.md").read_text() == "# Billing\n"
    ok2 = _invoke(write, path=".hubzoid/schedule/refresh/state.json", content="{}")
    assert "wrote" in ok2                                       # scratch always writable
    assert any(e["tool"] == "write_hub_file" for e in events)


def test_write_hub_file_refuses_everything_else(tmp_path):
    hub, _g, write, _e = _tool_env(tmp_path)
    (hub / "AGENTS.md").write_text("persona")
    for bad in ("AGENTS.md", ".env", "raw_data/core/f0.txt",
                "../outside.md", "/etc/passwd",
                ".hubzoid/schedule/other-task/state.json"):
        assert "refused" in _invoke(write, path=bad, content="x"), bad
    assert (hub / "AGENTS.md").read_text() == "persona"         # untouched


def test_write_hub_file_blocks_symlink_escape(tmp_path):
    hub, _g, write, _e = _tool_env(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (hub / "knowledge" / "link").symlink_to(outside)
    assert "refused" in _invoke(write, path="knowledge/link/x.md", content="leak")
    assert not (outside / "x.md").exists()


# ===========================================================================
# scheduler tick
# ===========================================================================
def _sched_env(tmp_path, *, busy=False):
    hub = tmp_path / "hub"
    _write_task(hub, "daily", 'schedule: "0 3 * * *"')
    fired = []

    async def fake_run(hub_dir, task):
        fired.append(task.name)
        sch.ScheduleState(hub_dir).record_fired(task.name, datetime.now(), result="done")
        return runner.RunResult(task=task.name, result="done", rounds=1)

    s = scheduler_lib.Scheduler(hub, is_busy=lambda: busy, run_task=fake_run)
    return hub, s, fired


def test_scheduler_fires_due_task_once(tmp_path):
    hub, s, fired = _sched_env(tmp_path)
    state = sch.ScheduleState(hub)
    state.record_fired("daily", datetime.now() - timedelta(days=2), result="done")
    assert asyncio.run(s.check_once()) == ["daily"]
    assert fired == ["daily"]
    assert asyncio.run(s.check_once()) == []                    # anchored: not due now


def test_scheduler_not_due_before_match(tmp_path):
    hub, s, fired = _sched_env(tmp_path)
    assert asyncio.run(s.check_once()) == []                    # first sight anchors now
    assert fired == []


def test_scheduler_defers_while_busy(tmp_path):
    hub, s, fired = _sched_env(tmp_path, busy=True)
    sch.ScheduleState(hub).record_fired("daily", datetime.now() - timedelta(days=2),
                                        result="done")
    assert asyncio.run(s.check_once()) == []
    assert fired == []
    s.is_busy = lambda: False                                   # hub goes idle
    assert asyncio.run(s.check_once()) == ["daily"]


def test_scheduler_skips_when_lock_held(tmp_path):
    hub, s, fired = _sched_env(tmp_path)
    sch.ScheduleState(hub).record_fired("daily", datetime.now() - timedelta(days=2),
                                        result="done")
    other = sch.RunLock(hub)
    assert other.acquire("manual-run")
    assert asyncio.run(s.check_once()) == []                    # lock-skip, not fired
    other.release()
    assert asyncio.run(s.check_once()) == ["daily"]


def test_scheduler_ignores_disabled_tasks(tmp_path):
    hub = tmp_path / "hub"
    _write_task(hub, "off", 'schedule: "0 3 * * *"\nenabled: false')
    sch.ScheduleState(hub).record_fired("off", datetime.now() - timedelta(days=2),
                                        result="done")
    fired = []

    async def fake_run(h, t):
        fired.append(t.name)
        return runner.RunResult(task=t.name, result="done")

    s = scheduler_lib.Scheduler(hub, run_task=fake_run)
    assert asyncio.run(s.check_once()) == [] and fired == []


def test_scheduler_disable_env_kills_start(tmp_path, monkeypatch):
    hub = tmp_path / "hub"
    _write_task(hub, "daily", 'schedule: "0 3 * * *"')
    monkeypatch.setenv(scheduler_lib.DISABLE_ENV, "1")

    async def go():
        return scheduler_lib.Scheduler(hub).start()

    assert asyncio.run(go()) is False


def test_scheduler_start_noop_without_tasks(tmp_path):
    async def go():
        return scheduler_lib.Scheduler(tmp_path).start()

    assert asyncio.run(go()) is False


# ===========================================================================
# CLI
# ===========================================================================
def test_cli_schedule_list_shows_task_and_next_fire(tmp_path):
    hub = tmp_path / "hub"
    _write_task(hub, "weekly", 'schedule: "7 3 * * 1"\ncommit: ["knowledge/"]')
    res = CliRunner().invoke(cli.app, ["schedule", "list", str(hub)])
    assert res.exit_code == 0, res.output
    assert "weekly" in res.output and "7 3 * * 1" in res.output
    assert "next" in res.output and "commit: knowledge" in res.output


def test_cli_schedule_list_surfaces_problems(tmp_path):
    hub = tmp_path / "hub"
    _write_task(hub, "broken", 'schedule: "99 * * * *"')
    res = CliRunner().invoke(cli.app, ["schedule", "list", str(hub)])
    assert res.exit_code == 1
    assert "broken" in res.output


def test_cli_schedule_run_dry_run_prints_prompt(tmp_path):
    hub = tmp_path / "hub"
    _write_task(hub, "job", 'schedule: "0 3 * * *"', body="Special marker body.")
    res = CliRunner().invoke(cli.app, ["schedule", "run", str(hub), "job", "--dry-run"])
    assert res.exit_code == 0, res.output
    assert "STATUS: DONE" in res.output and "Special marker body." in res.output


def test_cli_schedule_run_unknown_task(tmp_path):
    hub = tmp_path / "hub"
    _write_task(hub, "job", 'schedule: "0 3 * * *"')
    res = CliRunner().invoke(cli.app, ["schedule", "run", str(hub), "nope"])
    assert res.exit_code == 2
    assert "job" in res.output                                  # lists known tasks


def test_cli_schedule_run_executes_and_reports(tmp_path, monkeypatch):
    hub = tmp_path / "hub"
    _write_task(hub, "job", 'schedule: "0 3 * * *"')

    async def fake_run(hub_dir, task):
        return runner.RunResult(task=task.name, result="done", rounds=2,
                                summary="all synced", run_log=hub / "log.jsonl")

    monkeypatch.setattr("hubzoid.schedule_runner.run_task", fake_run)
    res = CliRunner().invoke(cli.app, ["schedule", "run", str(hub), "job"])
    assert res.exit_code == 0, res.output
    assert "done in 2 round(s)" in res.output and "all synced" in res.output


def test_cli_schedule_run_failure_exits_nonzero(tmp_path, monkeypatch):
    hub = tmp_path / "hub"
    _write_task(hub, "job", 'schedule: "0 3 * * *"')

    async def fake_run(hub_dir, task):
        return runner.RunResult(task=task.name, result="incomplete", rounds=10,
                                run_log=hub / "log.jsonl")

    monkeypatch.setattr("hubzoid.schedule_runner.run_task", fake_run)
    res = CliRunner().invoke(cli.app, ["schedule", "run", str(hub), "job"])
    assert res.exit_code == 1
    assert "incomplete" in res.output


def test_cli_schedule_status_shows_history(tmp_path):
    hub = tmp_path / "hub"
    _write_task(hub, "job", 'schedule: "0 3 * * *"')
    sch.ScheduleState(hub).record_fired("job", datetime(2026, 6, 8, 3, 0),
                                        result="done", run_log="/x/y.jsonl")
    res = CliRunner().invoke(cli.app, ["schedule", "status", str(hub)])
    assert res.exit_code == 0, res.output
    assert "job" in res.output and "done" in res.output and "y.jsonl" in res.output


# ===========================================================================
# template ships a (disabled) example task
# ===========================================================================
def test_minimal_template_ships_example_schedule():
    tpl = Path(__file__).resolve().parents[1] / "hubzoid" / "templates" / "minimal"
    example = tpl / "schedule" / "example.md"
    assert example.is_file()
    text = example.read_text()
    assert "schedule:" in text and "enabled: false" in text
    # gitignore covers runtime state
    assert ".hubzoid/" in (tpl / ".gitignore").read_text()
