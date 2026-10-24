# Hubzoid workflows. Apache-2.0 licensed like the rest of the repository.
"""Plain-language schedules → a cron string, evaluated in the workflow's zone.

The author writes a plain phrase (or a raw 5-field cron); we translate to cron
and evaluate it **in the workflow's own timezone** with croniter, so a "daily at
06:00 Asia/Kolkata" stays 06:00 local across DST rather than drifting. This is
the one scheduling mechanism (the per-minute dispatcher calls `next_after`).

Supported phrases (case-insensitive):
    "every 2 minutes"        -> */2 * * * *
    "every 15 min"           -> */15 * * * *
    "every 3 hours"          -> 0 */3 * * *
    "daily at 6am"           -> 0 6 * * *
    "daily 06:30"            -> 30 6 * * *
    "every monday 08:30"     -> 30 8 * * 1
    "* * * * *"  (raw cron)  -> passthrough
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone as _tz
from zoneinfo import ZoneInfo

from croniter import croniter

_DOW = {
    "sunday": 0, "sun": 0, "monday": 1, "mon": 1, "tuesday": 2, "tue": 2,
    "wednesday": 3, "wed": 3, "thursday": 4, "thu": 4, "friday": 5, "fri": 5,
    "saturday": 6, "sat": 6,
}


class ScheduleError(ValueError):
    """A schedule phrase that could not be parsed."""


def _parse_time(text: str) -> tuple[int, int]:
    """'6am' / '6' / '06:30' / '18:00' -> (hour, minute)."""
    text = text.strip().lower()
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if m:
        h, mm = int(m.group(1)), int(m.group(2))
    else:
        m = re.fullmatch(r"(\d{1,2})\s*(am|pm)?", text)
        if not m:
            raise ScheduleError(f"cannot parse time {text!r}")
        h, mm = int(m.group(1)), 0
        ap = m.group(2)
        if ap == "pm" and h != 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
    if not (0 <= h <= 23 and 0 <= mm <= 59):
        raise ScheduleError(f"time out of range: {text!r}")
    return h, mm


def to_cron(schedule: str) -> str:
    """Translate a plain-language schedule (or raw cron) to a 5-field cron string."""
    s = (schedule or "").strip().lower()
    if not s:
        raise ScheduleError("empty schedule")

    # raw 5-field cron passthrough (fields are only digits and * / , -)
    parts = s.split()
    if len(parts) == 5 and all(re.fullmatch(r"[\d*/,\-]+", p) for p in parts):
        if not croniter.is_valid(s):
            raise ScheduleError(f"invalid cron: {s}")
        return s

    m = re.fullmatch(r"every\s+(\d+)\s*(minute|minutes|min|m)", s)
    if m:
        n = int(m.group(1))
        if n < 1 or n > 59 or 60 % n:
            raise ScheduleError("minute interval must divide 60 (1–30); use every 1 hour for hourly")
        return f"*/{n} * * * *"

    m = re.fullmatch(r"every\s+(\d+)\s*(hour|hours|hr|h)", s)
    if m:
        n = int(m.group(1))
        if n < 1 or n > 23 or 24 % n:
            raise ScheduleError("hour interval must divide 24 (1–12); use daily for daily")
        return f"0 */{n} * * *"

    m = re.fullmatch(r"(daily|every day)(?:\s+at)?\s+(.+)", s)
    if m:
        h, mm = _parse_time(m.group(2))
        return f"{mm} {h} * * *"

    m = re.fullmatch(r"every\s+(\w+)(?:\s+at)?\s+(.+)", s)
    if m and m.group(1) in _DOW:
        dow = _DOW[m.group(1)]
        h, mm = _parse_time(m.group(2))
        return f"{mm} {h} * * {dow}"

    raise ScheduleError(f"unrecognized schedule: {schedule!r}")


def next_after(schedule: str, tz: str | None, after: datetime) -> datetime:
    """The next fire time strictly after `after`, evaluated in timezone `tz`
    (default UTC). Returns a timezone-aware datetime in `tz`.

    Cron runs on local wall-clock time, so the iteration happens on naive local
    times and each candidate is then placed in the zone and compared in UTC.
    On a day the clocks go back, the repeated hour therefore fires once; on a
    day they go forward, a time that doesn't exist fires at the first instant
    after the gap."""
    cron = to_cron(schedule)
    zone = ZoneInfo(tz) if tz else _tz.utc
    if after.tzinfo is None:
        after = after.replace(tzinfo=_tz.utc)
    after_utc = after.astimezone(_tz.utc)
    # Start a little earlier: a wall time skipped by a forward change lands
    # after `after` in UTC even though its local time reads earlier.
    local = (after_utc - timedelta(hours=3)).astimezone(zone).replace(tzinfo=None)
    it = croniter(cron, local)
    for _ in range(1000):
        candidate = it.get_next(datetime).replace(tzinfo=zone)
        if candidate.astimezone(_tz.utc) > after_utc:
            return candidate
    raise ScheduleError(f"no next time for {schedule!r}")


def due_between(schedule: str, tz: str | None, start: datetime, end: datetime) -> bool:
    """True if the schedule fires in the half-open window (start, end] — the
    per-minute dispatcher's check for 'is this workflow due since we last looked?'
    Compared in UTC, so two different instants with the same local wall time
    are never confused."""
    if end <= start:
        return False
    nxt = next_after(schedule, tz, start)
    return nxt.astimezone(_tz.utc) <= end.astimezone(_tz.utc)


_DAY_NAMES = ("Sundays", "Mondays", "Tuesdays", "Wednesdays", "Thursdays", "Fridays",
              "Saturdays")
_MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July", "August",
                "September", "October", "November", "December")


def describe(schedule: str) -> str:
    """The schedule in short words for people: "Mondays at 09:00", "Daily at
    06:00", "Every 15 minutes". A raw cron string reads as itself."""
    cron = to_cron(schedule)
    mm, hh, dom, mon, dow = cron.split()
    if hh == "*" and mm.startswith("*/") and dom == mon == dow == "*":
        n = int(mm[2:])
        return "Every minute" if n == 1 else f"Every {n} minutes"
    if mm == "0" and hh.startswith("*/") and dom == mon == dow == "*":
        n = int(hh[2:])
        return "Every hour" if n == 1 else f"Every {n} hours"
    if mm.isdigit() and hh.isdigit() and dom == mon == "*":
        at = f"{int(hh):02d}:{int(mm):02d}"
        if dow == "*":
            return f"Daily at {at}"
        if dow.isdigit() and 0 <= int(dow) <= 6:
            return f"{_DAY_NAMES[int(dow)]} at {at}"
        if dow == "1-5":
            return f"Weekdays at {at}"
    if mm.isdigit() and hh.isdigit() and dom.isdigit() and dow == "*":
        # Monthly and yearly (HUB-140 F19: these read as raw cron).
        at, day = f"{int(hh):02d}:{int(mm):02d}", int(dom)
        if mon == "*":
            return f"Day {day} of each month at {at}"
        if mon.isdigit() and 1 <= int(mon) <= 12:
            return f"Every year on {day} {_MONTH_NAMES[int(mon) - 1]} at {at}"
    return cron


def shortest_gap(schedule: str, tz: str | None = None) -> float:
    """The shortest time in seconds between two consecutive fires: what a
    minimum-interval rule compares against. Sampled over a few weeks, and
    across each daylight saving change of `tz`, where a day can be 23 hours."""
    starts = [datetime(2026, 1, 5, tzinfo=_tz.utc)]
    try:
        zone = ZoneInfo(tz) if tz else None
    except Exception:  # noqa: BLE001 — next_after reports a bad zone
        zone = None
    if zone is not None:
        day = datetime(2026, 1, 1, 12, tzinfo=_tz.utc)
        for _ in range(366):
            following = day + timedelta(days=1)
            if day.astimezone(zone).utcoffset() != following.astimezone(zone).utcoffset():
                starts.append(day - timedelta(days=35))
            day = following
    gaps = []
    for cursor in starts:
        end = cursor + timedelta(days=70)
        times = []
        for _ in range(60):
            cursor = next_after(schedule, tz, cursor)
            times.append(cursor.astimezone(_tz.utc))
            if cursor > end and len(times) > 1:
                break
        gaps += [(b - a).total_seconds() for a, b in zip(times, times[1:])]
    return min(gaps)
