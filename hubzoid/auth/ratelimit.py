"""Sign-in rate limits and lockout (``hz_auth_attempts``).

Failures are counted per client address (``ip:<address>``) and per account
email (``email:<address>``) in a 15-minute window. The tenth failure in a
window locks that key for 15 minutes; a locked key is refused before any
password is checked. A successful sign-in clears both keys.

Counters live in the shared operational store, so every bridge of a gateway
enforces the same limits. Each failure is one atomic upsert, safe across
processes on SQLite and PostgreSQL.

Loopback addresses are not counted per address: behind the edge without a
forwarded client address every browser would share one, and ten mistakes
anywhere would lock everyone out. The per-email limit still applies.
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


def retry_after(hub_dir: Path, *, ip: str | None = None, email: str | None = None,
                now: float | None = None) -> int:
    """Seconds until the caller may try again, or 0 when not locked."""
    wanted = keys(ip=ip, email=email)
    if not wanted:
        return 0
    now = time.time() if now is None else now
    with engine_for(Path(hub_dir)).connect() as conn:
        rows = conn.execute(sa.select(attempts.c.locked_until).where(
            attempts.c.key.in_(wanted), attempts.c.locked_until > now)).fetchall()
    if not rows:
        return 0
    return max(1, math.ceil(max(r[0] for r in rows) - now))


def record_failure(hub_dir: Path, *, ip: str | None = None, email: str | None = None,
                   now: float | None = None) -> int:
    """Count one failure against each key. Returns seconds locked (0 if not)."""
    now = time.time() if now is None else now
    limit = max_failures()
    engine = engine_for(Path(hub_dir))
    table = attempts.name
    # Every SET expression reads the row's values from before the update, on
    # both SQLite and PostgreSQL, so the window test is repeated per column.
    expired = f"{table}.window_start + :window <= :now"
    sql = sa.text(
        f"INSERT INTO {table} (key, window_start, failures, locked_until) "
        "VALUES (:key, :now, 1, CASE WHEN 1 >= :limit THEN :now + :lock ELSE NULL END) "
        "ON CONFLICT (key) DO UPDATE SET "
        f"failures = CASE WHEN {expired} THEN 1 ELSE {table}.failures + 1 END, "
        f"window_start = CASE WHEN {expired} THEN :now ELSE {table}.window_start END, "
        f"locked_until = CASE "
        f"WHEN {table}.locked_until IS NOT NULL AND {table}.locked_until > :now "
        f"THEN {table}.locked_until "
        f"WHEN (CASE WHEN {expired} THEN 1 ELSE {table}.failures + 1 END) >= :limit "
        "THEN :now + :lock ELSE NULL END"
    )
    locked = 0
    with engine.begin() as conn:
        for key in keys(ip=ip, email=email):
            conn.execute(sql, {"key": key, "now": now, "window": WINDOW_SECONDS,
                               "limit": limit, "lock": LOCK_SECONDS})
        wanted = keys(ip=ip, email=email)
        if wanted:
            rows = conn.execute(sa.select(attempts.c.locked_until).where(
                attempts.c.key.in_(wanted), attempts.c.locked_until > now)).fetchall()
            if rows:
                locked = max(1, math.ceil(max(r[0] for r in rows) - now))
    return locked


def record_success(hub_dir: Path, *, ip: str | None = None, email: str | None = None) -> None:
    """Clear the counters for this address and email after a sign-in."""
    wanted = keys(ip=ip, email=email)
    if not wanted:
        return
    with engine_for(Path(hub_dir)).begin() as conn:
        conn.execute(attempts.delete().where(attempts.c.key.in_(wanted)))
