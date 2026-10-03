"""Sign-in rate limits and lockout (``hz_auth_attempts``).

Attempts are counted per client address (``ip:<address>``) and per account
email (``email:<address>``) in a 15-minute window. Ten failures in a window
lock that key for 15 minutes; a locked key is refused before any password is
checked.

Admission is atomic: ``admit`` counts an attempt *before* its password is
checked, in one upsert per key, so a burst of parallel attempts (from one
bridge or several) cannot all slip past the check: the eleventh in a window is
refused and locks the key. ``record_failure`` then locks a key that reached
the limit; ``record_success`` clears the email's counter and takes the
successful attempt back off the address's counter (failures by others from
the same address keep counting, so a valid account cannot be used to reset a
password-spraying limit).

Self sign-up has a separate ``signup:<address>`` counter. It counts every
sign-up and is never refunded, so a run of successful sign-ups from one
address still hits the same window limit. Sign-in success does not touch it.

Counters live in the shared operational store, so every bridge of a gateway
enforces the same limits, on SQLite and PostgreSQL.

Loopback addresses are not counted per address: behind the edge without a
forwarded client address every browser would share one, and ten mistakes
anywhere would lock everyone out. The per-email limit still applies. The
client address is the one uvicorn derives from ``X-Forwarded-For`` set by a
trusted proxy in front of the bridge.
"""
from __future__ import annotations

import ipaddress
import math
import os
import time
from pathlib import Path

import sqlalchemy as sa

from .schema import attempts, engine_for

WINDOW_SECONDS = 15 * 60
LOCK_SECONDS = 15 * 60
MAX_FAILURES = 10


def _setting(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name) or default)
    except ValueError:
        return default
    return value if value > 0 else default


def max_failures() -> int:
    return _setting("HUBZOID_AUTH_MAX_FAILURES", MAX_FAILURES)


def _ip_key(ip: str | None) -> str | None:
    ip = (ip or "").strip()
    if not ip:
        return None
    try:
        if ipaddress.ip_address(ip).is_loopback:
            return None
    except ValueError:
        if ip in ("localhost", "testclient"):
            return None
    return "ip:" + ip[:64]


def _email_key(email: str | None) -> str | None:
    email = (email or "").strip().lower()
    return "email:" + email[:320] if email else None


def keys(*, ip: str | None = None, email: str | None = None) -> list[str]:
    return [k for k in (_ip_key(ip), _email_key(email)) if k]


def _signup_key(ip: str | None) -> str | None:
    """Per-address sign-up counter. Same address rules as ``_ip_key`` (loopback
    is not counted), but a distinct key so a successful sign-up cannot refund it."""
    ip_key = _ip_key(ip)
    if not ip_key:
        return None
    return "signup:" + ip_key[len("ip:"):]


def _wait(conn, wanted: list[str], now: float) -> int:
    rows = conn.execute(sa.select(attempts.c.locked_until).where(
        attempts.c.key.in_(wanted), attempts.c.locked_until > now)).fetchall()
    if not rows:
        return 0
    return max(1, math.ceil(max(r[0] for r in rows) - now))


def retry_after(hub_dir: Path, *, ip: str | None = None, email: str | None = None,
                now: float | None = None) -> int:
    """Seconds until the caller may try again, or 0 when not locked. Read-only."""
    wanted = keys(ip=ip, email=email)
    if not wanted:
        return 0
    now = time.time() if now is None else now
    with engine_for(Path(hub_dir)).connect() as conn:
        return _wait(conn, wanted, now)


def admit(hub_dir: Path, *, ip: str | None = None, email: str | None = None,
          now: float | None = None) -> int:
    """Count one attempt against each key, before its password is checked.
    Returns 0 to go ahead, or the seconds to wait when a key is locked (an
    attempt over the limit locks it too)."""
    wanted = keys(ip=ip, email=email)
    if not wanted:
        return 0
    return _admit_keys(hub_dir, wanted, now)


def admit_signup(hub_dir: Path, *, ip: str | None = None, now: float | None = None) -> int:
    """Count one sign-up against the address. Unlike sign-in, this counter is
    never refunded on success, so an address cannot create accounts without
    limit. Returns 0 to go ahead, or the seconds to wait when locked."""
    key = _signup_key(ip)
    if not key:
        return 0
    return _admit_keys(hub_dir, [key], now)


def _admit_keys(hub_dir: Path, wanted: list[str], now: float | None) -> int:
    now = time.time() if now is None else now
    table = attempts.name
    locked = f"({table}.locked_until IS NOT NULL AND {table}.locked_until > :now)"
    expired = f"({table}.window_start + :window <= :now)"
    counted = f"(CASE WHEN {expired} THEN 1 ELSE {table}.failures + 1 END)"
    # Every SET expression reads the row as it was before this statement, on
    # both SQLite and PostgreSQL, so shared terms are spelled out per column.
    sql = sa.text(
        f"INSERT INTO {table} (key, window_start, failures, locked_until) "
        "VALUES (:key, :now, 1, CASE WHEN 1 > :limit THEN :now + :lock ELSE NULL END) "
        "ON CONFLICT (key) DO UPDATE SET "
        f"failures = CASE WHEN {locked} THEN {table}.failures ELSE {counted} END, "
        f"window_start = CASE WHEN {locked} THEN {table}.window_start "
        f"WHEN {expired} THEN :now ELSE {table}.window_start END, "
        f"locked_until = CASE WHEN {locked} THEN {table}.locked_until "
        f"WHEN {counted} > :limit THEN :now + :lock ELSE NULL END"
    )
    params = {"now": now, "window": WINDOW_SECONDS, "limit": max_failures(), "lock": LOCK_SECONDS}
    with engine_for(Path(hub_dir)).begin() as conn:
        for key in wanted:
            conn.execute(sql, {**params, "key": key})
        return _wait(conn, wanted, now)


def record_failure(hub_dir: Path, *, ip: str | None = None, email: str | None = None,
                   now: float | None = None) -> int:
    """The attempt ``admit`` counted failed: lock every key that reached the
    limit. Returns the seconds locked (0 if not)."""
    wanted = keys(ip=ip, email=email)
    if not wanted:
        return 0
    now = time.time() if now is None else now
    with engine_for(Path(hub_dir)).begin() as conn:
        conn.execute(attempts.update().where(
            attempts.c.key.in_(wanted), attempts.c.failures >= max_failures(),
            sa.or_(attempts.c.locked_until.is_(None), attempts.c.locked_until <= now),
        ).values(locked_until=now + LOCK_SECONDS))
        return _wait(conn, wanted, now)


def record_success(hub_dir: Path, *, ip: str | None = None, email: str | None = None) -> None:
    """The attempt ``admit`` counted succeeded: clear the email's counter and
    take this attempt back off the address's counter."""
    email_key, ip_key = _email_key(email), _ip_key(ip)
    with engine_for(Path(hub_dir)).begin() as conn:
        if email_key:
            conn.execute(attempts.delete().where(attempts.c.key == email_key))
        if ip_key:
            conn.execute(attempts.update().where(
                attempts.c.key == ip_key, attempts.c.failures > 0,
            ).values(failures=attempts.c.failures - 1))
