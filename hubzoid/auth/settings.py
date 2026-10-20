"""Deployment-wide sign-in settings (``hz_auth_settings``), set in Settings or the CLI.

One small key/value table in the shared operational store, so every bridge of
a gateway reads the same values:

  * ``deployment_id``: the deployment's stable id. Stored once, the first time
    it is needed, with the value it already had (a hash of the deployment
    folder), so moving or restoring the folder elsewhere keeps the id and every
    record that carries it.
  * ``mfa_required``: who must use two-step sign-in: ``none``, ``admins``
    (the default) or ``all``. ``HUBZOID_MFA_REQUIRED`` gives the default
    before anyone sets it.

Reads are cached per process for a few seconds; a write clears the cache.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from .schema import engine_for, settings

log = logging.getLogger("hubzoid.auth")

MFA_POLICIES = ("none", "admins", "all")
_CACHE_SECONDS = 5.0
_cache: dict[tuple, tuple[float, str | None]] = {}
_lock = threading.Lock()


def _key(hub_dir: Path, name: str) -> tuple:
    return (id(engine_for(Path(hub_dir))), name)


def get(hub_dir: Path, name: str) -> str | None:
    key = _key(hub_dir, name)
    hit = _cache.get(key)
    if hit is not None and time.monotonic() - hit[0] < _CACHE_SECONDS:
        return hit[1]
    with engine_for(Path(hub_dir)).connect() as conn:
        row = conn.execute(sa.select(settings.c.value).where(settings.c.key == name)).first()
    value = row[0] if row else None
    _cache[key] = (time.monotonic(), value)
    return value


def put(hub_dir: Path, name: str, value: str, *, actor: str | None = None) -> None:
    now = time.time()
    engine = engine_for(Path(hub_dir))
    with engine.begin() as conn:
        done = conn.execute(settings.update().where(settings.c.key == name).values(
            value=value, updated_at=now, updated_by=actor)).rowcount
        if not done:
            conn.execute(settings.insert().values(key=name, value=value, updated_at=now,
                                                  updated_by=actor))
    _cache.pop(_key(hub_dir, name), None)


def setdefault(hub_dir: Path, name: str, value: str) -> str:
    """The stored value, storing ``value`` first when there is none. Safe when
    several bridges race: the first insert wins and everyone reads it."""
    found = get(hub_dir, name)
    if found is not None:
        return found
    engine = engine_for(Path(hub_dir))
    try:
        with engine.begin() as conn:
            conn.execute(settings.insert().values(key=name, value=value, updated_at=time.time(),
                                                  updated_by="system"))
    except IntegrityError:
        pass
    _cache.pop(_key(hub_dir, name), None)
    return get(hub_dir, name) or value


# ---- MFA policy -----------------------------------------------------------------

def mfa_policy(hub_dir: Path) -> str:
    stored = get(hub_dir, "mfa_required")
    if stored in MFA_POLICIES:
        return stored
    env = (os.environ.get("HUBZOID_MFA_REQUIRED") or "").strip().lower()
    return env if env in MFA_POLICIES else "admins"


def set_mfa_policy(hub_dir: Path, policy: str, *, actor: str) -> str:
    policy = (policy or "").strip().lower()
    if policy not in MFA_POLICIES:
        raise ValueError("Choose none, admins or all.")
    put(hub_dir, "mfa_required", policy, actor=actor)
    return policy


def mfa_required_for(hub_dir: Path, role: str) -> bool:
    policy = mfa_policy(hub_dir)
    return policy == "all" or (policy == "admins" and role == "admin")


def clear_cache() -> None:
    with _lock:
        _cache.clear()
