# Hubzoid access management. Apache-2.0 licensed like the rest of the repository.
"""Shared SQLite/Postgres access to Open WebUI's identity and OAuth tables.

Use OWUI's DATABASE_URL when configured; never fall back to a stale SQLite
file on connection failure. Read connections enforce read-only transactions.
Only OAuth refresh uses the explicitly separate write connection.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, URL, make_url
from sqlalchemy.pool import NullPool

log = logging.getLogger("hubzoid.access")


def db_path(hub_dir) -> Path:
    """Legacy SQLite path (also the gateway's OWUI data-directory hint)."""
    from .. import deployment

    configured = deployment.read(Path(hub_dir)).get("owui_db")
    override = os.environ.get("HUBZOID_OWUI_DB")
    return Path(configured or override or Path(hub_dir) / ".openwebui-data" / "webui.db")


def _normalize_url(value: str) -> URL:
    url = make_url(value)
    if url.drivername in {"postgres", "postgresql", "postgresql+asyncpg"}:
        url = url.set(drivername="postgresql+psycopg")
    if url.get_backend_name() not in {"sqlite", "postgresql"}:
        raise ValueError("Open WebUI identity storage requires SQLite or PostgreSQL")
    return url


def database_config(hub_dir) -> tuple[URL, str | None]:
    """Resolve the registered deployment or OWUI environment configuration."""
    from .. import deployment

    manifest = deployment.read(Path(hub_dir))
    registered = manifest.get("owui_database_url")
    explicit = os.environ.get("DATABASE_URL")
    schema = os.environ.get("DATABASE_SCHEMA") or None
    if registered:
        if explicit and _normalize_url(explicit) != _normalize_url(registered):
            raise ValueError("DATABASE_URL differs from the registered Open WebUI database")
        registered_schema = manifest.get("owui_database_schema") or None
        if schema and schema != registered_schema:
            raise ValueError("DATABASE_SCHEMA differs from the registered Open WebUI schema")
        return _normalize_url(registered), registered_schema
    if explicit:
        return _normalize_url(explicit), schema
    return URL.create("sqlite", database=str(db_path(hub_dir).resolve())), None


@lru_cache(maxsize=32)
def _engine(url: URL):
    # No idle DB connections or stale transaction snapshots between requests.
    # Engines cache dialect setup only; every caller must close its connection.
    args = {"timeout": 5.0} if url.get_backend_name() == "sqlite" else {"connect_timeout": 5}
    return create_engine(url, poolclass=NullPool, connect_args=args, hide_parameters=True)


def _connect(hub_dir, *, readonly: bool) -> Connection | None:
    con = None
    try:
        url, schema = database_config(hub_dir)
        if url.get_backend_name() == "sqlite":
            path = Path(url.database or "").resolve()
            if not path.is_file():
                return None
            # URI mode=rw also refuses accidental creation on the refresh path.
            url = URL.create("sqlite", database=path.as_uri(),
                             query={"mode": "ro" if readonly else "rw", "uri": "true"})
        con = _engine(url).connect()
        if url.get_backend_name() == "postgresql":
            if readonly:
                con.execute(text("SET TRANSACTION READ ONLY"))
            if schema:
                # One exact schema, transaction-local; never interpolate SQL.
                search_path = '"' + schema.replace('"', '""') + '"'
                con.execute(text("SELECT set_config('search_path', :schema, true)"),
                            {"schema": search_path})
        return con
    except Exception:
        if con is not None:
            con.close()
        # Driver exceptions can contain a password/URL. Do not log their text.
        log.warning("Open WebUI database unavailable or misconfigured; denying lookup")
        return None


def connect_ro(hub_dir) -> Connection | None:
    """Read-only connection, or None (fail closed). Caller must close it."""
    return _connect(hub_dir, readonly=True)


def connect_rw(hub_dir) -> Connection | None:
    """Existing DB connection for OAuth refresh only. Caller commits/closes."""
    return _connect(hub_dir, readonly=False)
