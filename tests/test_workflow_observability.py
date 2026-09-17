"""Scheduling observability: stale-dispatcher detection and restart downtime
reporting. Pure logic — no DBOS, no model, always runs."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from hubzoid.workflows import observe, runtime


def test_stale_detection():
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    # Fresh heartbeat -> not stale.
    assert observe._stale((now - timedelta(seconds=30)).isoformat(), now) is False
    # Old heartbeat -> stale.
    assert observe._stale((now - timedelta(minutes=10)).isoformat(), now) is True
    # Missing/corrupt heartbeat cannot report a scheduled dispatcher healthy.
    assert observe._stale(None, now) is True
    assert observe._stale("not-a-date", now) is True
    # Naive timestamps are treated as UTC.
    assert (
        observe._stale(
            (now - timedelta(minutes=10)).replace(tzinfo=None).isoformat(), now
        )
        is True
    )


def test_downtime_missed_counts_without_backfill(monkeypatch):
    # Two hourly workflows, one unscheduled (manual) — a 3h15m downtime.
    reg = {
        "hourly_a": SimpleNamespace(
            name="hourly_a", schedule="0 * * * *", timezone=None
        ),
        "hourly_b": SimpleNamespace(
            name="hourly_b", schedule="0 * * * *", timezone=None
        ),
        "manual": SimpleNamespace(name="manual", schedule=None, timezone=None),
    }
    monkeypatch.setattr(runtime, "_REGISTRY", reg)
    since = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    now = datetime(2026, 1, 1, 3, 15, 0, tzinfo=timezone.utc)
    window = runtime.downtime_missed(since, now)
    # 01:00, 02:00, 03:00 fired for each hourly schedule; manual contributes none.
    assert window["by_workflow"] == {"hourly_a": 3, "hourly_b": 3}
    assert window["missed"] == 6
    assert window["since"] == since.isoformat()
    assert window["until"] == now.isoformat()


def test_downtime_missed_empty_when_no_slots(monkeypatch):
    reg = {"daily": SimpleNamespace(name="daily", schedule="0 8 * * *", timezone=None)}
    monkeypatch.setattr(runtime, "_REGISTRY", reg)
    # A 30-minute window that contains no 08:00 slot.
    since = datetime(2026, 1, 1, 9, 0, 0, tzinfo=timezone.utc)
    now = datetime(2026, 1, 1, 9, 30, 0, tzinfo=timezone.utc)
    window = runtime.downtime_missed(since, now)
    assert window["missed"] == 0
    assert window["by_workflow"] == {}


def test_dispatch_failure_is_reported_after_trying_other_workflows(
    monkeypatch, tmp_path
):
    import pytest
    from hubzoid.access import store_for

    monkeypatch.setattr(runtime, "_HUB_DIR", tmp_path)
    monkeypatch.setattr(runtime, "_HUB_NAME", tmp_path.name)
    monkeypatch.setattr(
        runtime,
        "_REGISTRY",
        {
            name: SimpleNamespace(name=name, schedule="* * * * *", timezone=None)
            for name in ("broken", "healthy")
        },
    )
    called = []

    def start(name, **kwargs):
        called.append(name)
        if name == "broken":
            raise RuntimeError("database unavailable")

    monkeypatch.setattr(runtime, "start", start)
    now = datetime(2026, 1, 1, 0, 1, tzinfo=timezone.utc)
    with pytest.raises(RuntimeError, match="broken"):
        runtime.tick(last=now - timedelta(minutes=1), now=now)
    assert called == ["broken", "healthy"]
    assert (
        store_for(tmp_path).runtime_health(tmp_path.name)["last_dispatch"]
        == now.isoformat()
    )


def test_empty_dispatcher_releases_runtime(monkeypatch, tmp_path):
    import asyncio
    from hubzoid.workflows import boot

    stopped = []
    monkeypatch.setenv("HUBZOID_SCHEDULES", "1")
    monkeypatch.setattr(boot.Dispatcher, "prepare", lambda self: 0)
    monkeypatch.setattr(runtime, "shutdown", lambda: stopped.append(True))
    assert asyncio.run(boot.start(tmp_path)) is None
    assert stopped == [True]
