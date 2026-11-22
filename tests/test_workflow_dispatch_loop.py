"""The per-minute dispatcher of scheduled code workflows (#159 and friends).

The real `Dispatcher._loop` and `runtime.tick` run on a fake clock with a fake
store, so a simulated hour takes well under a second and no DBOS engine starts.
"""
from __future__ import annotations

import asyncio
import logging
import random
import threading
import types
from datetime import datetime, timedelta, timezone

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from hubzoid.workflows import boot, runtime

T0 = datetime(2026, 11, 20, 6, 0, 0, 500000, tzinfo=timezone.utc)   # 11:30:00.5 Asia/Kolkata


class FakeStore:
    def __init__(self):
        self.health: dict = {}
        self.hold = None
        self.paused: set[str] = set()

    def schedule_hold(self):
        if callable(self.hold):
            return self.hold()
        return self.hold

    def paused_workflows(self, hub):
        return set(self.paused)

    def runtime_health(self, hub):
        return dict(self.health)

    def update_runtime_health(self, hub, change):
        change(self.health)
        return dict(self.health)

    def set_runtime_health(self, hub, **values):
        self.health.update(values)


class Sim:
    """A fake wall clock driving the dispatcher. `wake(d)` is how much later
    than `d` a sleep returns (negative = early); `hop()` is what one thread
    hop (tick, health write) costs."""

    def __init__(self, monkeypatch, workflows, *, wake, hop, fail=None):
        self.now = T0.timestamp()
        self.store = FakeStore()
        self.fired: list[tuple[str, str]] = []
        self.fail = fail or set()
        clock = self

        class FakeDT(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.fromtimestamp(clock.now, tz or timezone.utc)

        async def sleep(d):
            clock.now += d + wake(d)
            await asyncio.sleep(0)

        async def to_thread(fn, *a, **k):
            clock.now += hop()
            return fn(*a, **k)

        def start(name, hub_name=None, *, scheduled_at=None, envelope=None):
            if name in clock.fail:
                raise ConnectionError("queue unavailable")
            clock.fired.append((name, scheduled_at))

        monkeypatch.setattr(boot, "asyncio", types.SimpleNamespace(
            sleep=sleep, to_thread=to_thread, CancelledError=asyncio.CancelledError,
            Event=asyncio.Event, create_task=asyncio.create_task))
        monkeypatch.setattr(boot, "datetime", FakeDT)
        import hubzoid.access as access
        monkeypatch.setattr(access, "store_for", lambda _d: clock.store)
        monkeypatch.setattr(runtime, "start", start)
        monkeypatch.setattr(runtime, "_HUB_DIR", "hub")
        monkeypatch.setattr(runtime, "_HUB_NAME", "hub")
        monkeypatch.setattr(runtime, "_DBOS", None)    # backlog check degrades quietly
        monkeypatch.setattr(boot, "_lift_stale_hold", lambda _d: False)
        monkeypatch.setattr(runtime, "_REGISTRY", {
            name: types.SimpleNamespace(name=name, schedule=sched, timezone="Asia/Kolkata",
                                        schedulable=False, catch_up=catch)
            for name, sched, catch in workflows})

    def run(self, minutes, *, resume_from=None, during=None):
        async def main():
            d = boot.Dispatcher("hub", "hub")
            d._task = None
            if resume_from is not None:
                d._last, d._resume = resume_from, True
                d._why = "the hub restarted"
            else:
                d._last = datetime.fromtimestamp(self.now, timezone.utc)
            task = asyncio.ensure_future(d._loop())
            end = self.now + minutes * 60
            while self.now < end:
                if during:
                    during(self)
                await asyncio.sleep(0)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        asyncio.run(main())

    def slots(self, name):
        return [datetime.fromisoformat(s) for n, s in self.fired if n == name]


def _every(minutes, start, end):
    first = (start.replace(second=0, microsecond=0) + timedelta(minutes=1))
    while first.minute % minutes:
        first += timedelta(minutes=1)
    out = []
    while first <= end:
        out.append(first)
        first += timedelta(minutes=minutes)
    return out


WORKFLOWS = [("classify", "every 1 minute", False), ("digest", "every 2 minutes", False)]


class _Warnings(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record):
        self.messages.append(record.getMessage())


@pytest.mark.slow
@settings(max_examples=20, deadline=None)
@given(seed=st.integers(0, 2**32 - 1), early=st.floats(0, 0.5))
def test_every_slot_fires_once_whatever_the_timer_jitter(seed, early):
    """#159: a timer that wakes up to 5 ms early (uvloop keeps whole
    milliseconds) or up to 2 s late, and thread hops of up to 100 ms, never
    cost a slot: one run per slot, in order, nothing counted as missed."""
    rnd = random.Random(seed)
    warnings = _Warnings()
    logger = logging.getLogger("hubzoid.workflows")
    logger.addHandler(warnings)
    try:
        with pytest.MonkeyPatch.context() as mp:
            sim = Sim(mp, WORKFLOWS,
                      wake=lambda d: -rnd.uniform(0, 0.005) if rnd.random() < early else rnd.uniform(0, 2),
                      hop=lambda: rnd.uniform(0, 0.1))
            sim.run(12)
    finally:
        logger.removeHandler(warnings)
    end = datetime.fromtimestamp(sim.now, timezone.utc)
    for name, every in (("classify", 1), ("digest", 2)):
        got = sim.slots(name)
        want = _every(every, T0, end)
        assert got[: len(want) - 1] == want[: len(want) - 1], name   # the last may be in flight
        assert len(got) == len(set(got))
    assert sim.store.health.get("missed", 0) == 0
    assert not [m for m in warnings.messages if "skipped" in m]


def test_an_hour_with_the_issues_jitter_fires_every_slot(monkeypatch):
    """The fast default-suite version of the property: #159's measured jitter
    (1 wake in 7 up to 3 ms early) for an hour, with a fixed seed."""
    rnd = random.Random(7)
    sim = Sim(monkeypatch, WORKFLOWS[:1],
              wake=lambda d: -rnd.uniform(0, 0.003) if rnd.random() < 1 / 7 else rnd.uniform(0.02, 0.15),
              hop=lambda: rnd.uniform(0.005, 0.03))
    sim.run(60)
    want = _every(1, T0, datetime.fromtimestamp(sim.now, timezone.utc))
    assert sim.slots("classify")[:59] == want[:59]
    assert sim.store.health.get("missed", 0) == 0


def test_the_old_loop_is_what_skipped(monkeypatch):
    """The same jitter on the pre-2.1.2 sleep (60 - now % 60, then tick with
    the wall clock) loses minutes. Guards the property above against a fake
    clock that cannot reproduce the bug."""
    rnd = random.Random(7)
    sim = Sim(monkeypatch, WORKFLOWS[:1],
              wake=lambda d: -rnd.uniform(0, 0.003) if rnd.random() < 1 / 7 else rnd.uniform(0.02, 0.15),
              hop=lambda: rnd.uniform(0.005, 0.03))
    fake = boot.datetime

    async def old_loop(self):
        while True:
            await boot.asyncio.sleep(max(0.1, 60 - fake.now(timezone.utc).timestamp() % 60))
            now = fake.now(timezone.utc)
            await boot.asyncio.to_thread(runtime.tick, last=self._last, now=now)
            self._last = now
            await boot.asyncio.to_thread(lambda: None)   # the health write
    monkeypatch.setattr(boot.Dispatcher, "_loop", old_loop)
    sim.run(60)
    assert sim.store.health.get("missed", 0) > 0


def test_a_blocked_loop_runs_the_latest_slot_and_says_why(monkeypatch, caplog):
    blocked = {"once": True}

    def wake(d):
        if blocked["once"]:
            blocked["once"] = False
            return 130.0          # the event loop was blocked for two minutes
        return 0.01
    sim = Sim(monkeypatch, WORKFLOWS[:1], wake=wake, hop=lambda: 0.01)
    with caplog.at_level(logging.WARNING, logger="hubzoid.workflows"):
        sim.run(4)
    assert sim.store.health["missed"] == 2
    assert [e[1:] for e in sim.store.health["missed_log"]] == [[2, "classify"]]
    warning = [r.getMessage() for r in caplog.records if "skipped 2" in r.getMessage()]
    assert warning and "did not look between" in warning[0]


def test_slots_inside_a_backup_hold_are_folded_with_the_hold_named(monkeypatch, caplog):
    sim = Sim(monkeypatch, WORKFLOWS[:1], wake=lambda d: 0.02, hop=lambda: 0.01)
    start = sim.now

    def during(s):
        s.store.hold = {"reason": "backup"} if start + 50 < s.now < start + 200 else None
    with caplog.at_level(logging.WARNING, logger="hubzoid.workflows"):
        sim.run(5, during=during)
    assert sim.store.health["missed"] >= 1
    assert any("held for a backup" in r.getMessage() for r in caplog.records)


def test_a_slot_that_cannot_be_queued_is_counted_and_others_still_start(monkeypatch, caplog):
    sim = Sim(monkeypatch, WORKFLOWS, wake=lambda d: 0.02, hop=lambda: 0.01, fail={"digest"})
    with caplog.at_level(logging.WARNING, logger="hubzoid.workflows"):
        sim.run(3)
    assert len(sim.slots("classify")) >= 2
    assert sim.slots("digest") == []
    assert runtime.missed_by_workflow(sim.store.health,
                                      datetime.fromtimestamp(sim.now, timezone.utc)) == {"digest": 1}
    assert any("could not be queued" in r.getMessage() for r in caplog.records)


def test_a_short_restart_runs_the_slot_inside_it_at_once(monkeypatch):
    """B2: the previous dispatcher's cursor is 40 s old and a minute boundary
    fell in between. The first tick runs at once and starts that slot."""
    sim = Sim(monkeypatch, WORKFLOWS[:1], wake=lambda d: 0.02, hop=lambda: 0.01)
    sim.now = (T0 + timedelta(seconds=20)).timestamp()        # 06:00:20.5
    cursor = T0 - timedelta(seconds=20)                        # 05:59:40.5
    sim.run(0.1, resume_from=cursor)
    assert sim.slots("classify") == [T0.replace(microsecond=0)]
    assert sim.store.health.get("missed", 0) == 0


def test_restart_resumes_a_recent_cursor_and_treats_an_old_one_as_downtime(monkeypatch, tmp_path):
    import hubzoid.access as access

    store = FakeStore()
    monkeypatch.setattr(access, "store_for", lambda _d: store)
    calls = {}

    class Disp:
        load_error = None
        owner = types.SimpleNamespace(heartbeat=lambda _r: None)

        def __init__(self, *a, **k):
            pass

        def prepare(self):
            return 1

        def start_loop(self, resume_from=None):
            calls["resume_from"] = resume_from

        async def _heartbeat(self):
            pass
        _events = _alerts = _heartbeat

    monkeypatch.setattr(boot, "Dispatcher", Disp)
    monkeypatch.setattr(boot, "schedules_enabled", lambda env=None: True)
    monkeypatch.setattr(boot, "markdown_work", lambda hub_dir, env=None: False)
    monkeypatch.setattr(runtime, "ready_record", lambda: {})
    monkeypatch.setattr(runtime, "downtime_missed", lambda since, now: {
        "since": since.isoformat(), "until": now.isoformat(), "missed": 3,
        "by_workflow": {"classify": 3}})
    monkeypatch.setattr(runtime, "catch_up", lambda since, now: [])
    import hubzoid.workflows.events as events
    monkeypatch.setattr(events, "declarations", lambda hub_dir: {})

    now = datetime.now(timezone.utc)
    store.health = {"last_tick": (now - timedelta(seconds=40)).isoformat(),
                    "heartbeat": now.isoformat()}
    asyncio.run(boot._start_engine(tmp_path, "hub"))
    assert calls["resume_from"] is not None
    assert not store.health.get("downtime")

    # A cursor 10 minutes old, with a fresh heartbeat from the ownership loop:
    # downtime, measured from the cursor, never silently dropped.
    store.health = {"last_tick": (now - timedelta(minutes=10)).isoformat(),
                    "heartbeat": now.isoformat()}
    asyncio.run(boot._start_engine(tmp_path, "hub"))
    # The loop starts from the downtime scan, so nothing falls between them.
    assert abs((calls["resume_from"] - now).total_seconds()) < 60
    assert store.health["downtime"]["missed"] == 3
    assert store.health["missed_log"][-1][1:] == [3, "classify"]


def test_downtime_and_catch_up_skip_paused_workflows_and_wait_out_a_hold(monkeypatch):
    sim = Sim(monkeypatch, [("classify", "every 1 minute", True),
                            ("digest", "every 2 minutes", True)],
              wake=lambda d: 0, hop=lambda: 0)
    since, now = T0, T0 + timedelta(minutes=10, seconds=30)
    sim.store.paused = {"digest"}
    window = runtime.downtime_missed(since, now)
    assert window["by_workflow"] == {"classify": 10}

    sim.store.hold = {"reason": "backup"}
    assert runtime.catch_up(since, now) == []
    sim.store.hold = None
    caught = runtime.catch_up(since, now)
    assert caught == ["classify"]                      # the paused one never runs
    assert runtime.without_caught_up(window, caught)["by_workflow"] == {"classify": 9}


def test_missed_log_keeps_workflow_names_and_old_entries():
    now = datetime(2026, 11, 22, tzinfo=timezone.utc)
    health = {"missed_log": [[(now - timedelta(days=2)).isoformat(), 4],            # before 2.1.2
                             [(now - timedelta(days=1)).isoformat(), 2, "classify"],
                             [(now - timedelta(days=40)).isoformat(), 9, "classify"]]}
    kept = runtime.missed_log(health, 0, now)               # a later zero-miss tick
    assert [e[1:] for e in kept] == [[4], [2, "classify"]]
    kept = runtime.missed_log({"missed_log": kept}, 1, now, name="digest")
    assert runtime.missed_by_workflow({"missed_log": kept}, now) == {"classify": 2, "digest": 1}


def _no_lost_health_updates(make_store):
    a, b = make_store(), make_store()
    a.set_runtime_health("hub", missed=0)
    stop = threading.Event()

    def beat():
        while not stop.is_set():
            b.set_runtime_health("hub", heartbeat="x")

    t = threading.Thread(target=beat)
    t.start()
    try:
        for _ in range(30):
            a.update_runtime_health("hub", lambda h: h.update(missed=h.get("missed", 0) + 1))
    finally:
        stop.set()
        t.join()
    assert a.runtime_health("hub")["missed"] == 30
    assert a.runtime_health("hub")["heartbeat"] == "x"


def test_runtime_health_updates_are_not_lost_across_stores(tmp_path):
    """B6: two store instances on one database (a bridge waiting for
    ownership writes the same record) never lose an update."""
    from sqlalchemy import create_engine

    from hubzoid.access.store import GrantStore

    url = f"sqlite:///{tmp_path / 'op.db'}"
    _no_lost_health_updates(lambda: GrantStore(create_engine(
        url, connect_args={"check_same_thread": False, "timeout": 30})))


@pytest.mark.slow
def test_runtime_health_updates_are_not_lost_on_postgres(fresh_postgres_url):
    from sqlalchemy import create_engine

    from hubzoid.access.store import GrantStore
    from hubzoid.db import sqlalchemy_url

    _no_lost_health_updates(lambda: GrantStore(create_engine(sqlalchemy_url(fresh_postgres_url))))


@pytest.mark.parametrize("waiting,warned", [(5, False), (6, True)])
def test_a_growing_queue_of_waiting_runs_is_reported(monkeypatch, caplog, waiting, warned):
    sim = Sim(monkeypatch, WORKFLOWS[:1], wake=lambda d: 0, hop=lambda: 0)
    monkeypatch.setattr(runtime, "_waiting_runs", lambda name: waiting)
    with caplog.at_level(logging.WARNING, logger="hubzoid.workflows"):
        runtime.tick(last=T0, now=T0 + timedelta(seconds=60))
    assert sim.slots("classify")                     # the slot still waits, never dropped
    assert any("waiting to start" in r.getMessage() for r in caplog.records) is warned
    shown = runtime.waiting_runs(sim.store.health, "classify")
    assert shown == (waiting if warned else None)
    if warned:   # a reading from before the workflow last dispatched is not shown
        later = datetime.now(timezone.utc) + timedelta(minutes=20)
        assert runtime.waiting_runs(sim.store.health, "classify", later) is None


def test_a_tick_that_runs_past_the_next_minute_still_runs_that_slot(monkeypatch):
    """A tick (or health write) that takes 70 s: the boundary that passed
    meanwhile is ticked right after, not skipped."""
    slow = {"once": 3}

    def hop():
        slow["once"] -= 1
        return 70.0 if slow["once"] == 0 else 0.01
    sim = Sim(monkeypatch, WORKFLOWS[:1], wake=lambda d: 0.02, hop=hop)
    sim.run(5)
    want = _every(1, T0, datetime.fromtimestamp(sim.now, timezone.utc))
    assert sim.slots("classify")[:4] == want[:4]
    assert sim.store.health.get("missed", 0) == 0


def test_a_tick_that_fails_before_looking_keeps_its_window(monkeypatch, caplog):
    """The store is unreachable for one tick: that window is not dropped, the
    next tick runs the latest slot and counts the other, saying why."""
    calls = {"n": 0}

    def hold():
        calls["n"] += 1
        if calls["n"] == 3:     # the next tick's first read (each tick reads twice)
            raise ConnectionError("database is locked")
        return None
    sim = Sim(monkeypatch, WORKFLOWS[:1], wake=lambda d: 0.02, hop=lambda: 0.01)
    sim.store.hold = hold
    with caplog.at_level(logging.WARNING, logger="hubzoid.workflows"):
        sim.run(4)
    assert sim.store.health["missed"] == 1
    assert any("dispatch at" in r.getMessage() and "failed" in r.getMessage()
               for r in caplog.records)


def test_the_sleep_never_ends_early_and_survives_a_clock_step_back(monkeypatch):
    now = {"t": 1000.0}
    slept = []

    async def sleep(d):
        slept.append(d)
        now["t"] += d - 0.0005 if len(slept) == 1 else d   # the first wake is early

    monkeypatch.setattr(boot, "_wall", lambda: now["t"])
    monkeypatch.setattr(boot, "asyncio", types.SimpleNamespace(sleep=sleep))
    assert asyncio.run(boot._sleep_until(1020.0)) == 1020.0
    assert now["t"] >= 1020.0 and len(slept) == 2       # slept the remainder
    now["t"] = 1020.0 - 3600                            # the clock stepped back an hour
    slept.clear()
    reached = asyncio.run(boot._sleep_until(1080.0))
    assert reached == boot._next_minute(1020.0 - 3600) and sum(slept) < 61


def test_a_failed_health_change_writes_nothing(tmp_path):
    from sqlalchemy import create_engine

    from hubzoid.access.store import GrantStore

    gs = GrantStore(create_engine(f"sqlite:///{tmp_path / 'op.db'}"))
    gs.set_runtime_health("hub", missed=3)

    def broken(health):
        health["missed"] = 99
        raise ValueError("bad record")
    with pytest.raises(ValueError):
        gs.update_runtime_health("hub", broken)
    assert gs.runtime_health("hub")["missed"] == 3
    gs.set_runtime_health("hub", missed=4)               # the store is not left locked
    assert gs.runtime_health("hub")["missed"] == 4


def test_a_backup_hold_is_lifted_only_when_no_backup_is_running(tmp_path, monkeypatch):
    """A bridge stopped mid-backup left its hold, which lasted hours and
    stopped every schedule. A free backup lock means no backup runs."""
    from hubzoid import _file_lock, backup, datadir
    from hubzoid.access import store_for
    from hubzoid.control import deploy

    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    hub = tmp_path / "hub"
    hub.mkdir()
    gs = store_for(hub)
    lock = deploy.state_root(hub) / datadir.STATE / "backup.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)

    gs.set_schedule_hold("backup", 3 * 3600, actor="schedule")
    with open(lock, "a+b") as fh:                       # a backup is running
        _file_lock.lock(fh)
        try:
            assert backup.clear_stale_hold(hub) is False
            assert gs.schedule_hold()
        finally:
            _file_lock.unlock(fh)
    assert backup.clear_stale_hold(hub) is True          # its process is gone
    assert gs.schedule_hold() is None

    gs.set_schedule_hold("maintenance", 60, actor="admin")
    assert backup.clear_stale_hold(hub) is False         # not a backup's hold
