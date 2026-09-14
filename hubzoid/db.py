"""One place to get a handle to the hub-owned database.

HubZoid owns the hub database. By default it is an embedded SQLite file at
``<hub>/.hubzoid/hub.db`` (zero config, single node). Set ``DATABASE_URL`` to a
Postgres URL to use a separately-hosted database instead — the same instance you
can point Open WebUI at, so there is one database for the hub.

We only ever create our OWN, ``hz_``-prefixed tables through this handle; we
never read or write Open WebUI's schema. Thin by design: SQLAlchemy Core gives
us one code path across SQLite and Postgres, no ORM and no models.
"""
from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

# Engines are meant to be singletons per URL (they hold the connection pool).
_engines: "dict[str, Engine]" = {}


def resolve_url(hub_dir, env=None) -> str:
    """The database URL for this hub: DATABASE_URL if set, else embedded SQLite."""
    env = env if env is not None else os.environ
    url = (env.get("DATABASE_URL") or "").strip()
    if url:
        return url
    db_path = Path(hub_dir) / ".hubzoid" / "hub.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{db_path}"


def operational_url(hub_dir, env=None) -> str:
    """The URL for the SHARED operational store — access grants, authority
    markers, identities, access-change audit, workflow state — that every bridge
    of a deployment must agree on.

    Precedence: ``HUBZOID_OPERATIONAL_DB`` (a gateway sets this to one shared
    SQLite file so all its bridges share one Casbin store) → ``DATABASE_URL``
    (Postgres, also shared) → the per-hub SQLite (standalone: same as the hub DB).

    Kept separate from the DBOS system DB (`dbos_url`), which stays per-bridge so
    N bridges never share one SQLite DBOS system database (unproven topology)."""
    env = env if env is not None else os.environ
    url = (env.get("HUBZOID_OPERATIONAL_DB") or env.get("DATABASE_URL") or "").strip()
    if url:
        return url
    return resolve_url(hub_dir, env)


def dbos_url(hub_dir, env=None) -> str:
    """The URL for this bridge's DBOS system database. PER-BRIDGE on SQLite (each
    hub gets its own file), so a gateway's N bridges do not share one SQLite DBOS
    system DB. Postgres (via ``HUBZOID_DBOS_DB`` or ``DATABASE_URL``) can be shared
    — DBOS supports many instances on one Postgres."""
    env = env if env is not None else os.environ
    url = (env.get("HUBZOID_DBOS_DB") or "").strip()
    if url:
        return url
    dl = (env.get("DATABASE_URL") or "").strip()
    if dl and not dl.startswith("sqlite"):
        return dl
    db_path = Path(hub_dir) / ".hubzoid" / "dbos.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{db_path}"


def engine_for(hub_dir, env=None) -> Engine:
    """Return the (cached) SQLAlchemy engine for this hub's database."""
    return _engine_for_url(resolve_url(hub_dir, env))


def operational_engine(hub_dir, env=None) -> Engine:
    """The (cached) engine for the shared operational store (see operational_url)."""
    return _engine_for_url(operational_url(hub_dir, env))


def _engine_for_url(url: str) -> Engine:
    eng = _engines.get(url)
    if eng is None:
        connect_args = {}
        if url.startswith("sqlite"):
            # Background tasks touch the engine from threadpool threads.
            connect_args["check_same_thread"] = False
        eng = create_engine(url, pool_pre_ping=True, connect_args=connect_args)
        _engines[url] = eng
    return eng
