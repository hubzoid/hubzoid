"""Property tests for download-link tokens (`hubzoid._signing`) and the cron
parser behind scheduled tasks (`hubzoid.scheduling`).

Contracts, from the code:
- A link token is an HMAC over chat, file name and, when links expire, the
  expiry. Any other chat, file or token is refused, and so is a past expiry.
  The expiry is read with ``int()``, so ``"0123"`` and ``"123"`` are one value.
- ``next_fire`` is the first minute strictly after ``after`` (naive local
  time) that the expression matches, or None when nothing matches within about
  two years.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from unittest import mock

from hypothesis import assume, given
from hypothesis import strategies as st

from hubzoid import _signing, scheduling

# ---------------------------------------------------------------------------
# Download-link tokens
# ---------------------------------------------------------------------------
_names = st.text(st.characters(exclude_categories=("Cs",), exclude_characters="/"), min_size=1, max_size=30)
_SECRET = {"HUBZOID_ARTIFACT_SECRET": "s" * 40, "HUBZOID_ARTIFACT_LINK_TTL": "3600"}


def _parse(query: str) -> dict[str, str]:
    return dict(part.split("=", 1) for part in query.split("&"))


@given(chat=_names, filename=_names, other=_names)
def test_issued_link_verifies_and_any_change_is_refused(chat, filename, other):
    with mock.patch.dict(os.environ, _SECRET):
        q = _parse(_signing.artifact_query(chat, filename))
        assert int(q["e"]) > time.time()
        assert _signing.verify_artifact_token(chat, filename, q["t"], q["e"])
        if other != chat:
            assert not _signing.verify_artifact_token(other, filename, q["t"], q["e"])
        if other != filename:
            assert not _signing.verify_artifact_token(chat, other, q["t"], q["e"])
        flipped = ("0" if q["t"][0] != "0" else "1") + q["t"][1:]
        assert not _signing.verify_artifact_token(chat, filename, flipped, q["e"])
        assert not _signing.verify_artifact_token(chat, filename, q["t"], str(int(q["e"]) + 1))
        assert not _signing.verify_artifact_token(chat, filename, q["t"], None)
        assert not _signing.verify_artifact_token(chat, filename, None, q["e"])
        assert not _signing.verify_artifact_token(chat, filename, q["t"], "soon")
        # The expiry is a number: leading zeros name the same instant.
        assert _signing.verify_artifact_token(chat, filename, q["t"], "0" + q["e"])


@given(chat=_names, filename=_names, ago=st.integers(1, 10**8))
def test_a_link_past_its_expiry_is_refused_even_with_a_valid_signature(chat, filename, ago):
    with mock.patch.dict(os.environ, _SECRET):
        expired = int(time.time()) - ago
        token = _signing._mac(chat, filename, expired, None)
        assert not _signing.verify_artifact_token(chat, filename, token, str(expired))


@given(chat=_names, filename=_names)
def test_never_expiring_links_carry_no_expiry_and_verify(chat, filename):
    with mock.patch.dict(os.environ, {**_SECRET, "HUBZOID_ARTIFACT_LINK_TTL": "0"}):
        q = _parse(_signing.artifact_query(chat, filename))
        assert set(q) == {"t"}
        assert _signing.verify_artifact_token(chat, filename, q["t"])
        assert q["t"] == _signing.sign_artifact_path(chat, filename)


# ---------------------------------------------------------------------------
# Cron expressions
# ---------------------------------------------------------------------------
_BOUNDS = {"minute": (0, 59), "hour": (0, 23), "dom": (1, 31), "month": (1, 12), "dow": (0, 6)}


@st.composite
def _field(draw, lo: int, hi: int):
    """A cron field and the set of values it means."""
    kind = draw(st.sampled_from(["star", "list", "range", "step"]))
    if kind == "star":
        return "*", set(range(lo, hi + 1))
    if kind == "list":
        values = draw(st.sets(st.integers(lo, hi), min_size=1, max_size=4))
        return ",".join(str(v) for v in sorted(values)), values
    a = draw(st.integers(lo, hi))
    b = draw(st.integers(a, hi))
    if kind == "range":
        return f"{a}-{b}", set(range(a, b + 1))
    step = draw(st.integers(1, 15))
    return f"{a}-{b}/{step}", set(range(a, b + 1, step))


@st.composite
def _cron(draw):
    parts, sets = [], []
    for lo, hi in _BOUNDS.values():
        text, values = draw(_field(lo, hi))
        parts.append(text)
        sets.append(values)
    return " ".join(parts), sets


@given(cron=_cron())
def test_cron_fields_parse_to_the_values_they_name(cron):
    expr, (minutes, hours, doms, months, dows) = cron
    c = scheduling.parse_cron(expr)
    assert (set(c.minutes), set(c.hours), set(c.doms), set(c.months), set(c.dows)) == (
        minutes, hours, doms, months, dows)
    assert c.dom_star == expr.split()[2].startswith("*")
    assert c.dow_star == expr.split()[4].startswith("*")


def _matches(c: scheduling.CronExpr, t: datetime) -> bool:
    return t.minute in c.minutes and t.hour in c.hours and scheduling._day_matches(c, t)


@given(cron=_cron(), after=st.datetimes(datetime(2026, 1, 1), datetime(2030, 12, 31)))
def test_next_fire_is_the_first_matching_minute_strictly_after(cron, after):
    c = scheduling.parse_cron(cron[0])
    got = scheduling.next_fire(c, after)
    # Brute force over the next three days, minute by minute.
    start = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    expected = next((start + timedelta(minutes=i) for i in range(3 * 24 * 60)
                     if _matches(c, start + timedelta(minutes=i))), None)
    if expected is not None:
        assert got == expected
    else:
        assume(got is not None)  # nothing in three days: only check what it found
        assert got > start + timedelta(days=3) - timedelta(minutes=1)
        assert _matches(c, got)


@given(minute=st.integers(0, 59), hour=st.integers(0, 23))
def test_day_of_week_seven_is_sunday(minute, hour):
    seven = scheduling.parse_cron(f"{minute} {hour} * * 7")
    zero = scheduling.parse_cron(f"{minute} {hour} * * 0")
    assert seven.dows == zero.dows == frozenset({0})
    monday = datetime(2026, 11, 23, 0, 0)  # a Monday
    fire = scheduling.next_fire(seven, monday)
    assert fire.weekday() == 6 and (fire.hour, fire.minute) == (hour, minute)
