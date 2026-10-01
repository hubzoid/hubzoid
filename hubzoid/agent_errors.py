"""What a person sees when an agent run fails for a reason they cannot fix.

A runtime that fails yields ``Notice(kind="error")`` (see ``hubzoid.run_events``).
``notice`` builds it from the raw error text and, for the ``claude`` CLI, its
stderr (a run that ends with a bare "exit code 1" names its cause only there).
``classify`` sorts the failure into one class:

  usage_limit  the model account reached its usage or rate limit (a session or
               weekly limit, HTTP 429)
  auth         the model login or key was refused (HTTP 401 or 403, an expired
               or missing login)
  overloaded   the model service is overloaded (HTTP 529)
  other        anything else

For the first three the person sees one plain sentence from ``MESSAGES`` on every
surface: alone in the web app's error box, and in place of ``[agent error: ...]``
on the text surfaces (the OpenAI-compatible endpoint, Telegram, WhatsApp, Slack).
A usage limit names the reset time in the hub's time zone (``hub_zone``) when the
error gives one. 'other' keeps the 1.0.x ``[agent error: ...]`` text. The class,
the reset time and the raw error go to the server log.

``notice`` also marks the request as failed (``_request_ctx.note_run_failure``):
the plain sentence has no ``[agent error:`` marker, and scheduled tasks and evals
must still record the run as failed.
"""
from __future__ import annotations

import logging
import math
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import _request_ctx
from .run_events import Notice

log = logging.getLogger("hubzoid.agent_errors")

USAGE_LIMIT = "usage_limit"
AUTH = "auth"
OVERLOADED = "overloaded"
OTHER = "other"
# The classes the person sees as a plain sentence.
PLAIN = (USAGE_LIMIT, AUTH, OVERLOADED)

# Everything the person reads, in one place. No stack trace and no product,
# account or token words: the person did nothing wrong and cannot fix it.
MESSAGES = {
    USAGE_LIMIT: ("The assistant has reached its usage limit for now. It will be available "
                  "again in a few hours. Please ask again after that."),
    "usage_limit_at": ("The assistant has reached its usage limit for now. It will be available "
                       "again {at} (in about {wait}). Please ask again after that."),
    AUTH: "The assistant is being reconnected by your admin team. Please try again later.",
    OVERLOADED: "The assistant is busy right now. Please try again in a minute.",
}

_STATUS_KINDS = {429: USAGE_LIMIT, 401: AUTH, 403: AUTH, 529: OVERLOADED}

_PATTERNS = (
    (USAGE_LIMIT, re.compile(
        r"hit your\b[^.\n]{0,40}?\blimit"        # "You've hit your session limit"
        r"|\blimit (?:reached|exceeded)\b"       # "5-hour limit reached", "usage limit reached|<epoch>"
        r"|\brate[ _-]?limit"                    # rate_limit_error, "rate limited"
        r"|\btoo many requests\b|\b429\b", re.I)),
    (AUTH, re.compile(
        r"\b40[13]\b|authentication[_ ]error|permission[_ ]error|\bunauthori[sz]ed\b"
        r"|invalid (?:api[ _-]?key|x-api-key|bearer token|oauth token|credentials)"
        r"|token (?:has )?(?:expired|been revoked)|\bnot logged in\b|please run /login", re.I)),
    (OVERLOADED, re.compile(r"\b529\b|overloaded", re.I)),
)

_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
# "resets 7:10am (UTC)", "resets Sep 18, 5am (UTC)", "resets 3pm".
_RESET = re.compile(
    r"\bresets?\s+(?:at\s+|on\s+)?"
    r"(?:(?P<month>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+"
    r"(?P<day>\d{1,2})(?:st|nd|rd|th)?,?\s+(?:at\s+)?)?"
    r"(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<ampm>[ap])\.?m\b\.?"
    r"(?:\s*\((?P<zone>[^)]+)\))?", re.I)
# Older CLIs: "Claude AI usage limit reached|1747040400".
_EPOCH = re.compile(r"limit reached\|(?P<epoch>\d{9,11})\b", re.I)


@dataclass(frozen=True)
class Failure:
    kind: str
    reset_at: datetime | None = None  # aware, in the hub's time zone


def hub_zone() -> tzinfo:
    """The hub's time zone: ``TZ`` when it names an IANA zone (``Asia/Kolkata``),
    set in the hub's ``.env`` or the service environment, else the server's own."""
    zone = _zone_named((os.environ.get("TZ") or "").lstrip(":"))
    return zone or datetime.now().astimezone().tzinfo


def _now(zone: tzinfo) -> datetime:
    return datetime.now(zone)


def _zone_named(name: str | None) -> tzinfo | None:
    name = (name or "").strip()
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def classify(text: str | None, *, status: int | None = None) -> str:
    """The class of a failure from its error text (and HTTP status, if known)."""
    if status in _STATUS_KINDS:
        return _STATUS_KINDS[status]
    for kind, pattern in _PATTERNS:
        if pattern.search(text or ""):
            return kind
    return OTHER


def reset_time(text: str | None, *, now: datetime | None = None,
               zone: tzinfo | None = None) -> datetime | None:
    """When a usage limit resets, from the error text, in ``zone`` (the hub's).
    None when the text names no time this understands."""
    zone = zone or hub_zone()
    now = (now or _now(zone)).astimezone(zone)
    text = text or ""
    m = _EPOCH.search(text)
    if m:
        return datetime.fromtimestamp(int(m["epoch"]), zone)
    m = _RESET.search(text)
    if not m:
        return None
    # A time without a zone is the CLI's local time, which follows TZ like ours.
    source = _zone_named(m["zone"]) if m["zone"] else zone
    hour, minute = int(m["hour"]), int(m["minute"] or 0)
    if source is None or not 1 <= hour <= 12 or minute > 59:
        return None
    hour = hour % 12 + (12 if m["ampm"].lower() == "p" else 0)
    here = now.astimezone(source)
    try:
        if m["month"]:
            at = here.replace(month=_MONTHS.index(m["month"][:3].lower()) + 1, day=int(m["day"]),
                              hour=hour, minute=minute, second=0, microsecond=0)
            if at < here - timedelta(days=180):  # "resets Jan 2" seen in late December
                at = at.replace(year=at.year + 1)
        else:
            at = here.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if at < here - timedelta(minutes=15):
                at += timedelta(days=1)
    except ValueError:
        return None
    return at.astimezone(zone)


def message(kind: str, reset_at: datetime | None = None, *, now: datetime | None = None) -> str:
    """The plain sentence for a class in ``PLAIN``."""
    if kind != USAGE_LIMIT or reset_at is None:
        return MESSAGES[kind]
    now = (now or _now(reset_at.tzinfo)).astimezone(reset_at.tzinfo)
    wait = reset_at - now
    if wait < timedelta(minutes=-15):
        return MESSAGES[USAGE_LIMIT]
    at = f"at {_clock(reset_at)}" if reset_at.date() == now.date() else (
        f"on {_MONTHS[reset_at.month - 1].title()} {reset_at.day} at {_clock(reset_at)}")
    return MESSAGES["usage_limit_at"].format(at=at, wait=_duration(wait))


def _clock(at: datetime) -> str:
    return f"{at.hour % 12 or 12}:{at.minute:02d} {'AM' if at.hour < 12 else 'PM'}"


def _duration(wait: timedelta) -> str:
    minutes = max(1, math.ceil(wait.total_seconds() / 60))
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} hr" + (f" {minutes} min" if minutes else "")
    days, hours = divmod(hours, 24)
    return (f"{days} day" if days == 1 else f"{days} days") + (f" {hours} hr" if hours else "")


def describe(detail: str, *, stderr: str = "", status: int | None = None,
             kind: str | None = None) -> Failure:
    """Classify a failure. The error text decides; the CLI's stderr only when the
    text says nothing (a bare "exit code 1"). ``kind`` skips classification when
    the runtime already knows the class."""
    if kind is None:
        kind = classify(detail, status=status)
        if kind == OTHER and stderr:
            kind = classify(stderr)
    reset = None
    if kind == USAGE_LIMIT:
        reset = reset_time(detail) or (reset_time(stderr) if stderr else None)
    return Failure(kind=kind, reset_at=reset)


def notice(detail: str, *, stderr: str = "", status: int | None = None,
           kind: str | None = None) -> Notice:
    """The error notice for a failed run (``detail`` is the raw error), logged and
    recorded as this request's failure. 'other' is the 1.0.x notice unchanged."""
    failure = describe(detail, stderr=stderr, status=status, kind=kind)
    reset = failure.reset_at.isoformat() if failure.reset_at else None
    _request_ctx.note_run_failure(failure.kind, reset_at=reset)
    log.warning("agent run failed: class=%s reset=%s error=%s", failure.kind, reset or "-", detail)
    if failure.kind not in PLAIN:
        return Notice(kind="error", text=detail, legacy=f"\n\n[agent error: {detail}]")
    text = message(failure.kind, failure.reset_at)
    return Notice(kind="error", text=text, legacy=f"\n\n{text}", error_kind=failure.kind)
