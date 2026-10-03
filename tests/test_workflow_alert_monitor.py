"""Model-free alert delivery and workflow health contracts."""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from hubzoid.workflows import alerts, events, monitor, runtime


@pytest.fixture
def hub(tmp_path, monkeypatch):
    for key in ("DATABASE_URL", "HUBZOID_DBOS_DB", "HUBZOID_OPERATIONAL_DB", "HUBZOID_DEPLOYMENT"):
        monkeypatch.delenv(key, raising=False)
    (tmp_path / "workflows").mkdir()
    (tmp_path / "workflows" / "settings.yaml").write_text(
        "alerts:\n  to:\n    - webhook: TEST_ALERT_URL\n"
        "  failures_in_a_row: 2\n  pause_after_failures: 2\n"
    )
    return tmp_path


def _rows(hub):
    with events._engine(hub).connect() as conn:
        return conn.execute(text("SELECT kind,state,attempt,due FROM hz_workflow_alerts ORDER BY created")).all()


def test_outbox_deduplicates_and_retries_with_injected_sender(hub):
    alerts.record(hub, hub.name, "run_failed", "run-1", {"workflow": "check", "run_id": "run-1"})
    alerts.record(hub, hub.name, "run_failed", "run-1", {"workflow": "check", "run_id": "run-1"})
    with events._engine(hub).connect() as conn:
        aid = conn.execute(text("SELECT id FROM hz_workflow_alerts")).scalar_one()
    calls = []

    async def sender(_, row):
        calls.append(row["id"])
        if len(calls) == 1:
            raise TimeoutError("planned")

    asyncio.run(alerts.deliver(hub, aid, sender=sender))
    failed = _rows(hub)
    assert len(failed) == 1 and failed[0].state == "pending"
    assert failed[0].attempt == 1 and failed[0].due > time.time()
    asyncio.run(alerts.deliver(hub, aid, sender=sender))
    asyncio.run(alerts.deliver(hub, aid, sender=sender))
    assert calls == [aid, aid]
    assert _rows(hub)[0].state == "sent"


def test_concurrent_delivery_claim_sends_once(hub):
    alerts.record(hub, hub.name, "run_failed", "run-2", {"workflow": "check", "run_id": "run-2"})
    with events._engine(hub).connect() as conn:
        aid = conn.execute(text("SELECT id FROM hz_workflow_alerts")).scalar_one()
    calls = []

    async def race():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def sender(_, row):
            calls.append(row["id"])
            entered.set()
            await release.wait()

        first = asyncio.create_task(alerts.deliver(hub, aid, sender=sender))
        await asyncio.wait_for(entered.wait(), 5)
        second = asyncio.create_task(alerts.deliver(hub, aid, sender=sender))
        await asyncio.wait_for(second, 5)
        assert len(calls) == 1
        assert _rows(hub)[0].state == "sending"
        release.set()
        await asyncio.wait_for(first, 5)

    asyncio.run(race())
    assert calls == [aid]
    assert _rows(hub)[0].state == "sent" and _rows(hub)[0].attempt == 1


def test_expired_claim_is_recoverable(hub):
    alerts.record(hub, hub.name, "run_failed", "run-3", {"workflow": "check", "run_id": "run-3"})
    with events._engine(hub).begin() as conn:
        aid = conn.execute(text("SELECT id FROM hz_workflow_alerts")).scalar_one()
        conn.execute(text("UPDATE hz_workflow_alerts SET state='sending',attempt=1,due=:past WHERE id=:id"),
                     {"past": time.time() - 1, "id": aid})
    alerts.recover_deliveries(hub)
    assert _rows(hub)[0].state == "pending" and _rows(hub)[0].attempt == 1
    calls = []

    async def sender(_, row):
        calls.append(row["id"])

    asyncio.run(alerts.deliver(hub, aid, sender=sender))
    assert calls == [aid]
    assert _rows(hub)[0].state == "sent" and _rows(hub)[0].attempt == 2


class _Store:
    def __init__(self):
        self.enabled = False
        self.paused = set()

    def paused_workflows(self, _):
        return self.paused

    def schedule_hold(self):
        return False

    def runtime_health(self, _):
        return {"enabled": self.enabled}


def _run(name, run_id, status, when, trigger=None, created=None):
    """A finished DBOS run; times are seconds, stored in milliseconds like DBOS."""
    return SimpleNamespace(name=name, workflow_id=run_id, status=status,
                           completed_at=int(when * 1000), updated_at=int(when * 1000),
                           created_at=int((created or when) * 1000),
                           attributes={"trigger": trigger} if trigger else {})


def _reconcile_with(hub, monkeypatch, rows, calls=None):
    """DBOS's listing filtered by completion time (inclusive, as DBOS does)."""
    from datetime import datetime
    from dbos import DBOS
    from hubzoid import access
    store = _Store()

    def list_workflows(*, completed_after=None, completed_before=None, **_):
        lo = datetime.fromisoformat(completed_after).timestamp() * 1000 if completed_after else float("-inf")
        hi = datetime.fromisoformat(completed_before).timestamp() * 1000 if completed_before else float("inf")
        if calls is not None:
            calls.append((lo, hi))
        return [r for r in rows if lo <= r.completed_at <= hi]

    monkeypatch.setattr(runtime, "_OWNER", SimpleNamespace(assert_owned=lambda: None))
    monkeypatch.setattr(DBOS, "list_workflows", list_workflows)
    monkeypatch.setattr(access, "store_for", lambda _: store)
    monkeypatch.setattr(alerts, "dispatch", lambda *_: None)
    monkeypatch.setattr(alerts, "overdue", lambda: None)
    monkeypatch.setattr(runtime, "registry", lambda: [])
    return store


def _cursor_at(hub, when):
    from hubzoid.workflows.state import WorkflowState
    WorkflowState(events._engine(hub), hub.name, "__alerts__")["cursor"] = {
        "t": when * 1000, "recent": {}}


def test_failure_during_an_outage_longer_than_a_day_still_alerts(hub, monkeypatch):
    from hubzoid.workflows.markdown import EVAL_WORKFLOW
    now = time.time()
    _cursor_at(hub, now - 72 * 3600)   # the engine last looked three days ago
    _reconcile_with(hub, monkeypatch, [_run(EVAL_WORKFLOW, "eval:old", "ERROR", now - 48 * 3600)])
    for _ in range(4):   # an outage this long catches up over a few passes
        alerts.reconcile(hub, hub.name)
    assert [row.kind for row in _rows(hub)] == ["eval_failed"]


def test_incremental_scan_counts_each_run_once_by_completion_time(hub, monkeypatch):
    now = time.time()
    _cursor_at(hub, now - 60)
    rows = [
        # Created ten days ago, finished just now: completion time decides.
        _run("sync", "r1", "ERROR", now - 30, created=now - 10 * 86400),
        _run("sync", "r2", "ERROR", now - 20),
    ]
    calls = []
    _reconcile_with(hub, monkeypatch, rows, calls)
    alerts.reconcile(hub, hub.name)
    first = len(calls)
    alerts.reconcile(hub, hub.name)   # nothing new: no double count, no full scan
    from hubzoid.workflows.state import WorkflowState
    state = WorkflowState(events._engine(hub), hub.name, "__alerts__")
    assert state.get("streak:sync")["n"] == 2
    assert [row.kind for row in _rows(hub)] == ["run_failed", "consecutive_failures"]
    assert all(lo >= (now - 60 - 300) * 1000 for lo, _ in calls[first:])
    # A commit that lands late with an earlier timestamp is still seen once.
    rows.append(_run("sync", "late", "SUCCESS", now - 25))
    alerts.reconcile(hub, hub.name)
    alerts.reconcile(hub, hub.name)
    assert state.get("streak:sync")["n"] == 0


def test_only_consecutive_scheduled_failures_auto_pause(hub, monkeypatch):
    now = time.time()
    _cursor_at(hub, now - 60)
    md = "md:digest:{}@hz-hub"
    rows = [
        _run("scheduled", "s1", "ERROR", now - 9, "schedule"),
        _run("manual", "m1", "ERROR", now - 8, "manual"),
        _run("manual", "m2", "ERROR", now - 7, "manual"),
        _run(events.COORDINATOR, "wh:event:r0", "ERROR", now - 6),
        _run(events.COORDINATOR, "wh:event2:r0", "ERROR", now - 5),
        # Markdown: only cron slots count, not legacy webhook or manual runs.
        _run("hz_markdown_task", md.format("events-20260101T0900-abc"), "ERROR", now - 4.5),
        _run("hz_markdown_task", md.format("events-20260101T0901-def"), "ERROR", now - 4.4),
        _run("hz_markdown_task", md.format("manual-20260101T090000"), "ERROR", now - 4.3),
        _run("scheduled", "s2", "ERROR", now - 4, "schedule"),
    ]
    store = _reconcile_with(hub, monkeypatch, rows)
    from hubzoid.workflows import control
    paused = []
    monkeypatch.setattr(control, "set_paused",
                        lambda _, name, value, **kw: paused.append((name, value, kw["prefer"])))
    alerts.reconcile(hub, hub.name)
    assert paused == [("scheduled", True, "code")]
    # Resumed, then a manual run succeeds: the schedule is not paused again.
    rows.append(_run("scheduled", "m3", "SUCCESS", now - 3, "manual"))
    alerts.reconcile(hub, hub.name)
    assert paused == [("scheduled", True, "code")]
    rows += [_run("hz_markdown_task", md.format("20260101T0905"), "ERROR", now - 2),
             _run("hz_markdown_task", md.format("20260101T0906") + ":requeued", "ERROR", now - 1)]
    alerts.reconcile(hub, hub.name)
    assert paused[-1] == ("md:digest", True, "markdown")


def test_failed_event_alerts_once_without_rescanning(hub, monkeypatch):
    eid = events.admit(hub, hub.name, "ticket", "handle_ticket", "T-1",
                       {"body": {"id": "T-1"}, "headers": {}})
    row = events.get(hub, eid)
    monkeypatch.setattr(runtime, "_OWNER", SimpleNamespace(assert_owned=lambda: None))
    events.fail(hub, hub.name, row, "RuntimeError")
    assert events.get(hub, eid)["state"] == "failed"
    # The coordinator run that ends in error carries the same source id.
    now = time.time()
    _cursor_at(hub, now - 60)
    _reconcile_with(hub, monkeypatch, [_run(events.COORDINATOR, eid + ":r0", "ERROR", now - 1)])
    alerts.reconcile(hub, hub.name)
    assert [row.kind for row in _rows(hub)] == ["run_failed"]


def test_monitor_emits_one_stale_and_one_recovered_event(hub, monkeypatch):
    from hubzoid import access
    store = _Store()
    store.enabled = True
    monkeypatch.setattr(access, "store_for", lambda _: store)
    assert not monitor.inspect(hub)
    assert not monitor.inspect(hub)
    assert [row.kind for row in _rows(hub)] == ["engine_stale"]
    with events._engine(hub).begin() as conn:
        conn.execute(text("INSERT INTO hz_workflow_owner (hub,boot,generation,expires,ready) "
                          "VALUES (:h,'boot',1,:e,:r)"),
                     {"h": hub.name, "e": time.time() + 90, "r": json.dumps({"version": "v1"})})
    assert monitor.inspect(hub)
    assert monitor.inspect(hub)
    assert [row.kind for row in _rows(hub)] == ["engine_stale", "engine_recovered"]
