"""Tests for scheduled evals — the second task source on the existing scheduler.

Covers the fork (schedule/ -> agent harness, evals/ -> deterministic runner),
per-case anchors, batching, the idle gate, the run lock, and the fact that a
failing suite records its anchor so it waits for the next cron match instead
of re-firing every tick.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from hubzoid import scheduler as scheduler_lib
from hubzoid.evals import schedule as evals_schedule
from hubzoid.evals.results import CaseResult, SuiteResult
from hubzoid.scheduling import RunLock, ScheduleState


EVERY_MINUTE = "* * * * *"


def _hub(tmp_path: Path, evals: dict[str, str] | None = None,
         tasks: dict[str, str] | None = None) -> Path:
    hub = tmp_path / "hub"
    (hub / "evals").mkdir(parents=True)
    (hub / "schedule").mkdir(parents=True)
    (hub / "AGENTS.md").write_text("---\nname: H\ndescription: D\n---\nBe good.\n")
    for name, text in (evals or {}).items():
        (hub / "evals" / name).write_text(text, encoding="utf-8")
    for name, text in (tasks or {}).items():
        (hub / "schedule" / name).write_text(text, encoding="utf-8")
    return hub


def _scheduled(cron: str = EVERY_MINUTE, body: str = "hi") -> str:
    return f'---\nschedule: "{cron}"\n---\n{body}'


def _now() -> datetime:
    """Wall clock pinned to a minute boundary.

    Cron matching truncates to the minute, so a test that fires at HH:MM:45 and
    then re-ticks 30s later crosses into the next minute and is legitimately due
    again. Pinning seconds to 0 makes these tests deterministic instead of
    passing or failing depending on the time of day.
    """
    return datetime.now().replace(second=0, microsecond=0)


class _Recorder:
    """Stands in for evals.schedule.run_due; records what it was asked to run."""

    def __init__(self, ok: bool = True):
        self.calls: list[list[str]] = []
        self.ok = ok

    async def __call__(self, hub_dir, due, state, *, now=None):
        self.calls.append([c.name for c in due])
        suite = SuiteResult(hub=hub_dir.name)
        for c in due:
            r = CaseResult(name=c.name)
            if not self.ok:
                r.error = "boom"
            suite.cases.append(r)
        now = now or datetime.now()
        for c in due:
            state.record_fired(evals_schedule.state_key(c), now,
                               result="pass" if self.ok else "fail")
        return suite


def _sched(hub, recorder, *, busy=False):
    return scheduler_lib.Scheduler(
        hub, is_busy=lambda: busy, run_evals=recorder,
        run_task=lambda *a, **k: (_ for _ in ()).throw(AssertionError("agent harness ran")),
    )


# ---------------------------------------------------------------------------
# discovery + due-ness
# ---------------------------------------------------------------------------
def test_only_cases_with_a_schedule_are_scheduled(tmp_path):
    hub = _hub(tmp_path, {"timed.md": _scheduled(), "manual.md": "hi"})
    assert [c.name for c in evals_schedule.scheduled_cases(hub)] == ["timed"]


def test_disabled_case_is_not_scheduled(tmp_path):
    hub = _hub(tmp_path, {"off.md": f'---\nschedule: "{EVERY_MINUTE}"\nenabled: false\n---\nhi'})
    assert evals_schedule.scheduled_cases(hub) == []


def test_a_broken_case_file_does_not_stop_the_others(tmp_path):
    """One unparseable file must not wedge the schedule."""
    hub = _hub(tmp_path, {"good.md": _scheduled(), "bad.md": "---\nbogus: 1\n---\nhi"})
    assert [c.name for c in evals_schedule.scheduled_cases(hub)] == ["good"]


def test_first_discovery_anchors_now_and_is_not_due(tmp_path):
    """A case installed now fires at its next future match, not retroactively."""
    hub = _hub(tmp_path, {"c.md": _scheduled("0 6 * * 1")})
    state = ScheduleState(hub)
    assert evals_schedule.due_cases(hub, state, _now()) == []


def test_becomes_due_once_the_cron_matches(tmp_path):
    hub = _hub(tmp_path, {"c.md": _scheduled()})
    state = ScheduleState(hub)
    now = _now()
    assert evals_schedule.due_cases(hub, state, now) == []          # anchors
    later = now + timedelta(minutes=5)
    assert [c.name for c in evals_schedule.due_cases(hub, state, later)] == ["c"]


def test_state_keys_are_namespaced(tmp_path):
    """An eval case and a scheduled task may share a name without colliding."""
    hub = _hub(tmp_path, {"nightly.md": _scheduled()})
    (case,) = evals_schedule.scheduled_cases(hub)
    assert evals_schedule.state_key(case) == "eval:nightly"


def test_eval_and_task_of_the_same_name_keep_separate_anchors(tmp_path):
    hub = _hub(tmp_path, {"nightly.md": _scheduled()})
    state = ScheduleState(hub)
    now = _now()
    evals_schedule.due_cases(hub, state, now)                       # anchors eval:nightly
    later = now + timedelta(minutes=5)
    # The scheduled *task* named `nightly` fires; the eval's anchor must be
    # untouched, so the eval is still due.
    state.record_fired("nightly", later, result="done")
    assert [c.name for c in evals_schedule.due_cases(hub, state, later)] == ["nightly"]
    assert state.get("eval:nightly").get("last_fired_at") is None


# ---------------------------------------------------------------------------
# the scheduler fork
# ---------------------------------------------------------------------------
def test_due_evals_fire_through_the_eval_runner(tmp_path):
    """Not the agent harness — run_task raises if it is ever called."""
    hub = _hub(tmp_path, {"c.md": _scheduled()})
    rec = _Recorder()
    sched = _sched(hub, rec)
    now = _now()
    asyncio.run(sched.check_once(now))                              # anchors
    fired = asyncio.run(sched.check_once(now + timedelta(minutes=5)))
    assert fired == ["eval:c"]
    assert rec.calls == [["c"]]


def test_all_due_cases_run_as_one_suite(tmp_path):
    """Batched so five cases on one cron cost one runtime build, not five."""
    hub = _hub(tmp_path, {"a.md": _scheduled(), "b.md": _scheduled(),
                          "c.md": _scheduled()})
    rec = _Recorder()
    sched = _sched(hub, rec)
    now = _now()
    asyncio.run(sched.check_once(now))
    asyncio.run(sched.check_once(now + timedelta(minutes=5)))
    assert rec.calls == [["a", "b", "c"]]


def test_unscheduled_cases_never_fire(tmp_path):
    hub = _hub(tmp_path, {"manual.md": "hi"})
    rec = _Recorder()
    sched = _sched(hub, rec)
    asyncio.run(sched.check_once(_now() + timedelta(days=2)))
    assert rec.calls == []


def test_busy_hub_defers_the_run(tmp_path):
    """An eval must never start mid-conversation."""
    hub = _hub(tmp_path, {"c.md": _scheduled()})
    rec = _Recorder()
    now = _now()
    asyncio.run(_sched(hub, rec).check_once(now))
    asyncio.run(_sched(hub, rec, busy=True).check_once(now + timedelta(minutes=5)))
    assert rec.calls == []


def test_deferred_run_fires_once_the_hub_is_idle(tmp_path):
    hub = _hub(tmp_path, {"c.md": _scheduled()})
    rec = _Recorder()
    now = _now()
    asyncio.run(_sched(hub, rec).check_once(now))
    later = now + timedelta(minutes=5)
    asyncio.run(_sched(hub, rec, busy=True).check_once(later))
    asyncio.run(_sched(hub, rec).check_once(later))
    assert rec.calls == [["c"]]


def test_run_lock_blocks_a_concurrent_run(tmp_path):
    hub = _hub(tmp_path, {"c.md": _scheduled()})
    rec = _Recorder()
    sched = _sched(hub, rec)
    now = _now()
    asyncio.run(sched.check_once(now))

    other = RunLock(hub)
    assert other.acquire("someone-else")
    try:
        assert asyncio.run(sched.check_once(now + timedelta(minutes=5))) == []
    finally:
        other.release()
    assert rec.calls == []


def test_a_failing_suite_still_records_the_anchor(tmp_path):
    """Otherwise a persistently failing case re-fires on every single tick."""
    hub = _hub(tmp_path, {"c.md": _scheduled()})
    rec = _Recorder(ok=False)
    sched = _sched(hub, rec)
    now = _now()
    asyncio.run(sched.check_once(now))
    fire_at = now + timedelta(minutes=5)
    asyncio.run(sched.check_once(fire_at))
    # Same minute, next tick: the anchor moved to fire_at, so the every-minute
    # cron's next match is fire_at+1min and the case is not due again yet.
    asyncio.run(sched.check_once(fire_at + timedelta(seconds=30)))
    assert rec.calls == [["c"]]
    assert ScheduleState(hub).get("eval:c")["last_result"] == "fail"


def test_a_crashing_eval_run_does_not_kill_the_tick_loop(tmp_path):
    hub = _hub(tmp_path, {"c.md": _scheduled()})

    async def boom(*a, **k):
        raise RuntimeError("model down")

    sched = _sched(hub, boom)
    now = _now()
    asyncio.run(sched.check_once(now))
    asyncio.run(sched.check_once(now + timedelta(minutes=5)))      # must not raise


def test_lock_is_released_after_a_crash(tmp_path):
    hub = _hub(tmp_path, {"c.md": _scheduled()})

    async def boom(*a, **k):
        raise RuntimeError("model down")

    sched = _sched(hub, boom)
    now = _now()
    asyncio.run(sched.check_once(now))
    asyncio.run(sched.check_once(now + timedelta(minutes=5)))
    assert RunLock(hub).acquire("after")


# ---------------------------------------------------------------------------
# start()
# ---------------------------------------------------------------------------
def test_scheduler_starts_for_evals_with_no_schedule_tasks(tmp_path):
    """A hub whose only background work is evals must still start the loop."""
    hub = _hub(tmp_path, {"c.md": _scheduled()})

    async def go():
        sched = _sched(hub, _Recorder())
        started = sched.start()
        await sched.stop()
        return started

    assert asyncio.run(go()) is True


def test_scheduler_does_not_start_with_nothing_to_do(tmp_path):
    hub = _hub(tmp_path, {"manual.md": "hi"})

    async def go():
        return _sched(hub, _Recorder()).start()

    assert asyncio.run(go()) is False


# ---------------------------------------------------------------------------
# run_due
# ---------------------------------------------------------------------------
def test_run_due_persists_json_and_records_state(tmp_path, monkeypatch):
    from hubzoid.evals import report as report_lib

    hub = _hub(tmp_path, {"c.md": f'---\nschedule: "{EVERY_MINUTE}"\ncontains: [ok]\n---\nhi'})

    class FakeRuntime:
        name = "fake"

        async def aopen(self): ...
        async def aclose(self): ...
        async def run(self, prompt): return "ok"

    monkeypatch.setattr("hubzoid.runtime.build", lambda *a, **k: FakeRuntime())
    state = ScheduleState(hub)
    due = evals_schedule.scheduled_cases(hub)
    suite = asyncio.run(evals_schedule.run_due(hub, due, state))

    assert suite.ok
    assert report_lib.latest(hub).passed == 1
    assert state.get("eval:c")["last_result"] == "pass"


def test_run_due_logs_failures_at_error(tmp_path, monkeypatch, caplog):
    """A scheduled eval nobody looks at manufactures confidence — failures
    must reach whatever the operator already watches."""
    hub = _hub(tmp_path, {"c.md": f'---\nschedule: "{EVERY_MINUTE}"\ncontains: [pong]\n---\nhi'})

    class FakeRuntime:
        name = "fake"

        async def aopen(self): ...
        async def aclose(self): ...
        async def run(self, prompt): return "nope"

    monkeypatch.setattr("hubzoid.runtime.build", lambda *a, **k: FakeRuntime())
    with caplog.at_level("ERROR", logger="hubzoid.evals"):
        asyncio.run(evals_schedule.run_due(hub, evals_schedule.scheduled_cases(hub),
                                           ScheduleState(hub)))
    assert any("FAILED" in r.message for r in caplog.records)
