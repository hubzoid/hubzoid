"""Unit tests for the plain-language schedule grammar (pure, no DBOS)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from hubzoid.workflows.schedule_grammar import (
    ScheduleError,
    due_between,
    next_after,
    to_cron,
)


@pytest.mark.parametrize("phrase,cron", [
    ("every 2 minutes", "*/2 * * * *"),
    ("every 15 min", "*/15 * * * *"),
    ("every 3 hours", "0 */3 * * *"),
    ("daily at 6am", "0 6 * * *"),
    ("daily 06:30", "30 6 * * *"),
    ("daily at 6pm", "0 18 * * *"),
    ("every monday 08:30", "30 8 * * 1"),
    ("every Friday at 17:00", "0 17 * * 5"),
    ("*/5 * * * *", "*/5 * * * *"),          # raw cron passthrough
    ("30 6 * * 1", "30 6 * * 1"),
])
def test_to_cron(phrase, cron):
    assert to_cron(phrase) == cron


def test_bad_schedule_raises():
    with pytest.raises(ScheduleError):
        to_cron("whenever I feel like it")
    with pytest.raises(ScheduleError):
        to_cron("")


def test_next_after_in_zone_is_dst_safe():
    # 06:00 India time is 00:30 UTC year-round (IST has no DST) — the point is
    # that we evaluate in-zone, not by freezing a UTC cron.
    after = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
    nxt = next_after("daily 06:00", "Asia/Kolkata", after)
    assert (nxt.hour, nxt.minute) == (6, 0)
    assert str(nxt.tzinfo) == "Asia/Kolkata"


def test_next_after_utc_default():
    after = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    nxt = next_after("every 15 min", None, after)
    assert nxt == datetime(2026, 1, 1, 10, 15, tzinfo=timezone.utc)


def test_due_between_window():
    start = datetime(2026, 1, 1, 10, 0, 30, tzinfo=timezone.utc)
    end = datetime(2026, 1, 1, 10, 1, 5, tzinfo=timezone.utc)
    assert due_between("* * * * *", None, start, end)      # 10:01 falls in window
    # a window with no minute boundary
    s2 = datetime(2026, 1, 1, 10, 0, 10, tzinfo=timezone.utc)
    e2 = datetime(2026, 1, 1, 10, 0, 40, tzinfo=timezone.utc)
    assert not due_between("* * * * *", None, s2, e2)
