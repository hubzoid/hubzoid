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


def test_daylight_saving_changes_fire_each_day_once():
    from datetime import datetime, timedelta, timezone

    from hubzoid.workflows.schedule_grammar import due_between

    def fires(schedule, tz, start, hours):
        t, out = start, []
        for _ in range(hours * 60):
            if due_between(schedule, tz, t, t + timedelta(minutes=1)):
                out.append(t + timedelta(minutes=1))
            t += timedelta(minutes=1)
        return [f"{x:%m-%d %H:%M}" for x in out]

    utc = timezone.utc
    # Clocks go back: 01:30 happens twice on 1 Nov 2026 in New York, and runs once.
    assert fires("daily at 01:30", "America/New_York", datetime(2026, 10, 31, 4, tzinfo=utc), 52) == [
        "10-31 05:30", "11-01 05:30", "11-02 06:30"]
    # Clocks go forward: 02:30 doesn't exist on 8 Mar 2026; it runs right after the gap.
    assert fires("daily at 02:30", "America/New_York", datetime(2026, 3, 7, 5, tzinfo=utc), 52) == [
        "03-07 07:30", "03-08 07:30", "03-09 06:30"]


def test_schedules_read_as_short_words():
    from hubzoid.workflows.schedule_grammar import describe, shortest_gap

    assert describe("every monday at 09:00") == "Mondays at 09:00"
    assert describe("daily at 6pm") == "Daily at 18:00"
    assert describe("every 15 minutes") == "Every 15 minutes"
    assert shortest_gap("every 2 hours") == 7200
