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


def engine_for(hub_dir, env=None) -> Engine:
    """Return the (cached) SQLAlchemy engine for this hub's database."""
    url = resolve_url(hub_dir, env)
    eng = _engines.get(url)
    if eng is None:
        connect_args = {}
        if url.startswith("sqlite"):
            # Background tasks touch the engine from threadpool threads.
            connect_args["check_same_thread"] = False
        eng = create_engine(url, pool_pre_ping=True, connect_args=connect_args)
        _engines[url] = eng
    return eng
