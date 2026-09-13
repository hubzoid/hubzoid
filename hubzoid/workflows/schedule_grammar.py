# Hubzoid workflows. MIT licensed like the rest of the repository.
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
from datetime import datetime, timezone as _tz
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
        return s

    m = re.fullmatch(r"every\s+(\d+)\s*(minute|minutes|min|m)", s)
    if m:
        n = int(m.group(1))
        return f"*/{n} * * * *"

    m = re.fullmatch(r"every\s+(\d+)\s*(hour|hours|hr|h)", s)
    if m:
        n = int(m.group(1))
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
    (default UTC). Returns a timezone-aware datetime in `tz`."""
    cron = to_cron(schedule)
    zone = ZoneInfo(tz) if tz else _tz.utc
    if after.tzinfo is None:
        after = after.replace(tzinfo=_tz.utc)
    local_after = after.astimezone(zone)
    it = croniter(cron, local_after)
    return it.get_next(datetime)


def due_between(schedule: str, tz: str | None, start: datetime, end: datetime) -> bool:
    """True if the schedule fires in the half-open window (start, end] — the
    per-minute dispatcher's check for 'is this workflow due since we last looked?'"""
    if end <= start:
        return False
    nxt = next_after(schedule, tz, start)
    return nxt <= end.astimezone(nxt.tzinfo)
