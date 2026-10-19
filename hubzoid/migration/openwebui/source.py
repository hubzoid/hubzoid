# Hubzoid migration. Apache-2.0 licensed like the rest of the repository.
"""Read-only SQLite/Postgres access to an Open WebUI (1.x) database, for
``hubzoid migrate openwebui`` and the upgrade checks.

Where the database is: the deployment's recorded ``owui_db`` /
``owui_database_url`` (a gateway that ran Open WebUI), else ``DATABASE_URL``,
else ``HUBZOID_OWUI_DB``, else ``<hub>/.openwebui-data/webui.db``. Never falls
back to a stale SQLite file on connection failure, and every connection runs
read-only transactions.
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, URL, make_url
from sqlalchemy.pool import NullPool

log = logging.getLogger("hubzoid.migration")


def db_path(hub_dir) -> Path:
    """The SQLite path (also the hint for Open WebUI's data directory)."""
    from ... import deployment

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
    from ... import deployment

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


def connect_ro(hub_dir) -> Connection | None:
    """Read-only connection, or None (fail closed). Caller must close it."""
    con = None
    try:
        url, schema = database_config(hub_dir)
        if url.get_backend_name() == "sqlite":
            path = Path(url.database or "").resolve()
            if not path.is_file():
                return None
            url = URL.create("sqlite", database=path.as_uri(), query={"mode": "ro", "uri": "true"})
        con = _engine(url).connect()
        if url.get_backend_name() == "postgresql":
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
        log.warning("Open WebUI database unavailable or misconfigured")
        return None
