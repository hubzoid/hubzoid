"""Database handles: one shared operational store per deployment.

Gateway discovery also works from operator CLI commands. OWUI owns its account
schema; SQLite DBOS databases remain per hub. PostgreSQL can share a server/DB.
Only Hubzoid tables are owned here; DBOS owns its execution schema.
"""
from __future__ import annotations

import os
from pathlib import Path

from . import datadir

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
    db_path = datadir.state(hub_dir, env) / "hub.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{db_path}"


def operational_url(hub_dir, env=None) -> str:
    """The URL for the SHARED operational store — access grants, authority
    markers, identities, access-change audit, workflow state — that every bridge
    of a deployment must agree on.

    Precedence: the registered deployment manifest (`.hubzoid/deployment.json`,
    written by the gateway and discovered by bridges and operator CLI commands)
    is authoritative — so a CLI grant lands in the same store the running gateway
    reads. A ``HUBZOID_OPERATIONAL_DB`` that disagrees with the manifest is a
    misconfiguration and raises rather than silently splitting the store. With no
    manifest (standalone): ``HUBZOID_OPERATIONAL_DB`` → ``DATABASE_URL`` (Postgres,
    also shared) → the per-hub SQLite (same as the hub DB).

    Kept separate from the DBOS system DB (`dbos_url`), which stays per-bridge so
    N bridges never share one SQLite DBOS system database (unproven topology)."""
    env = env if env is not None else os.environ
    from .deployment import read
    configured = read(Path(hub_dir), env).get("operational_url")
    explicit = (env.get("HUBZOID_OPERATIONAL_DB") or "").strip()
    if configured:
        if explicit and explicit != configured:
            raise ValueError("HUBZOID_OPERATIONAL_DB differs from the registered deployment. Update the gateway configuration, not an individual bridge.")
        return configured
    return explicit or (env.get("DATABASE_URL") or "").strip() or resolve_url(hub_dir, env)


def shared_store_dir(hub_dir, env=None, *, cwd=None) -> "Path | None":
    """The folder of ``shared_store_file``, or None."""
    path = shared_store_file(hub_dir, env, cwd=cwd)
    return path.parent if path is not None else None


def shared_store_file(hub_dir, env=None, *, cwd=None) -> "Path | None":
    """The shared operational store's file, when it is a SQLite file outside
    the hub folder (in a gateway's data folder). None otherwise.

    A process that uses the store must be able to write there: SQLite keeps
    its journal next to the file, and Hubzoid its migration lock. The URL is
    ``operational_url``'s, so a manifest that disagrees with
    ``HUBZOID_OPERATIONAL_DB`` raises ValueError here too. A relative SQLite
    path is taken from ``cwd`` (the process's working directory by default)."""
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import ArgumentError

    from .deployment import manifest_path

    env = env if env is not None else os.environ
    configured = (manifest_path(Path(hub_dir), env)
                  or (env.get("HUBZOID_OPERATIONAL_DB") or "").strip()
                  or (env.get("DATABASE_URL") or "").strip())
    if not configured:
        return None  # the hub's own database, inside the hub
    try:
        url = make_url(sqlalchemy_url(operational_url(hub_dir, env)))
    except (OSError, KeyError, ArgumentError):
        return None
    if url.get_backend_name() != "sqlite" or url.database in (None, "", ":memory:"):
        return None
    path = Path(url.database)
    if not path.is_absolute():
        path = Path(cwd if cwd is not None else Path.cwd()) / path
    path = path.resolve()
    if path.parent.is_relative_to(Path(hub_dir).resolve()):
        return None
    return path


def writable_paths(hub_dir) -> str:
    """What a hub's sandboxed service may write (systemd ``ReadWritePaths``):
    the hub folder, and in a gateway the shared store's folder. Read with the
    hub's own ``.env`` over this environment, as the service will run, and
    relative to the hub (the unit's working directory)."""
    env = dict(os.environ)
    dotenv = Path(hub_dir) / ".env"
    if dotenv.is_file():
        from dotenv import dotenv_values

        env.update({k: v for k, v in dotenv_values(dotenv).items() if v is not None})
    shared = shared_store_dir(hub_dir, env, cwd=hub_dir)
    return " ".join(str(p) for p in (Path(hub_dir), shared) if p is not None)


def dbos_url(hub_dir, env=None) -> str:
    """The URL for this bridge's DBOS system database. PER-BRIDGE on SQLite (each
    hub gets its own file), so a gateway's N bridges do not share one SQLite DBOS
    system DB. Postgres (via ``HUBZOID_DBOS_DB`` or ``DATABASE_URL``) can be shared
    — DBOS supports many instances on one Postgres."""
    env = env if env is not None else os.environ
    from .deployment import read
    for hub in read(Path(hub_dir), env).get('hubs', []):
        if Path(hub['path']).resolve() == Path(hub_dir).resolve() and hub.get('dbos_url'):
            explicit = (env.get('HUBZOID_DBOS_DB') or '').strip()
            if explicit and explicit != hub['dbos_url']:
                raise ValueError('HUBZOID_DBOS_DB differs from the registered deployment')
            return hub['dbos_url']
    url = (env.get("HUBZOID_DBOS_DB") or "").strip()
    if url:
        return url
    dl = (env.get("DATABASE_URL") or "").strip()
    if dl and not dl.startswith("sqlite"):
        return dl
    db_path = datadir.state(hub_dir, env) / "dbos.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{db_path}"


def engine_for(hub_dir, env=None) -> Engine:
    """Return the (cached) SQLAlchemy engine for this hub's database."""
    return _engine_for_url(resolve_url(hub_dir, env))


SQLITE_LOCK_WAIT_SECONDS = 30


def operational_engine(hub_dir, env=None) -> Engine:
    """The (cached) engine for the shared operational store (see operational_url)."""
    return _engine_for_url(operational_url(hub_dir, env))


def sqlalchemy_url(url: str) -> str:
    """The URL with an explicit driver for Hubzoid's own engines.

    ``postgresql://`` and ``postgres://`` mean psycopg (version 3), which
    Hubzoid installs. Without this, SQLAlchemy picks psycopg2, which only the
    Open WebUI extra brought in, so a 1.0.x ``DATABASE_URL`` would stop
    working after the upgrade. Other URLs are returned unchanged."""
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def _engine_for_url(url: str) -> Engine:
    url = sqlalchemy_url(url)
    eng = _engines.get(url)
    if eng is None:
        connect_args = {}
        if url.startswith("sqlite"):
            # Background tasks touch the engine from threadpool threads.
            connect_args["check_same_thread"] = False
            # A gateway's bridges share one file. Wait for a writer's lock
            # rather than failing a request after SQLite's default 5 s.
            connect_args["timeout"] = SQLITE_LOCK_WAIT_SECONDS
        eng = create_engine(url, pool_pre_ping=True, connect_args=connect_args)
        _engines[url] = eng
    return eng
