"""Facts about an install being upgraded from Open WebUI (Hubzoid 1.0.x).

Used by `hubzoid run` (the upgrade guard) and `hubzoid doctor`. Read-only: no
database or file is created, no migration runs, and no database URL or password
is ever returned.

  * openwebui_database(hub)  where the hub's Open WebUI database is, resolved as
                             hubzoid.access.owui_db resolves it
  * openwebui_accounts(hub)  (where, number of accounts) when it has any
  * hubzoid_accounts(hub)    accounts in the Hubzoid web app (hz_users)
  * people(conn)             the same count on an open operational store
                             connection (the gateway's upgrade guard)

This module deliberately does not import hubzoid.access: that package loads the
agent SDKs, which would add seconds to every `hubzoid run`.
"""
from __future__ import annotations

import os
from pathlib import Path


def openwebui_database(hub: Path):
    """(SQLAlchemy URL, schema, where) of the Open WebUI database this hub used,
    resolved as hubzoid.access.owui_db does (the deployment's registered
    database, then DATABASE_URL, then the SQLite file) without importing
    hubzoid.access, which loads the agent SDKs and would slow every start."""
    from sqlalchemy.engine import URL, make_url

    from . import deployment

    try:
        manifest = deployment.read(hub)
    except Exception:  # noqa: BLE001 - a malformed manifest is reported by the bridge
        manifest = {}
    raw = manifest.get("owui_database_url") or os.environ.get("DATABASE_URL")
    if raw:
        url = make_url(raw)
        if url.drivername in {"postgres", "postgresql", "postgresql+asyncpg"}:
            url = url.set(drivername="postgresql+psycopg")
        schema = (manifest.get("owui_database_schema") if manifest.get("owui_database_url")
                  else os.environ.get("DATABASE_SCHEMA")) or None
        if url.get_backend_name() != "sqlite":
            return url, schema, "the Open WebUI database in DATABASE_URL"
        return url, None, str(url.database or "")
    path = Path(manifest.get("owui_db") or os.environ.get("HUBZOID_OWUI_DB")
                or hub / ".openwebui-data" / "webui.db")
    return URL.create("sqlite", database=str(path)), None, str(path)


def openwebui_accounts(hub: Path) -> tuple[str, int] | None:
    """(where, number of accounts) of an Open WebUI database this hub used, or
    None. Read-only; never creates a file or prints a database URL."""
    from sqlalchemy import create_engine, func, inspect, select, table
    from sqlalchemy.pool import NullPool

    try:
        url, schema, where = openwebui_database(hub)
        if url.get_backend_name() == "sqlite":
            path = Path(url.database or "")
            if not path.is_file():
                return None
            url = url.set(database=path.resolve().as_uri(), query={"mode": "ro", "uri": "true"})
            args = {"timeout": 5.0}
        else:
            args = {"connect_timeout": 5}
        engine = create_engine(url, poolclass=NullPool, connect_args=args, hide_parameters=True)
    except Exception:  # noqa: BLE001 - not a database we can read: nothing to move
        return None
    try:
        with engine.connect() as con:
            if not inspect(con).has_table("user", schema=schema):
                return None
            people = con.execute(select(func.count()).select_from(
                table("user", schema=schema))).scalar() or 0
    except Exception:  # noqa: BLE001 - unreachable or unreadable: the bridge reports it
        return None
    finally:
        engine.dispose()
    return (where, int(people)) if people else None


def hubzoid_accounts(hub: Path) -> int:
    """Accounts in the Hubzoid web app (hz_users): 0 before the first start, and
    0 when the operational store cannot be read (the bridge reports that)."""
    from sqlalchemy import create_engine, inspect
    from sqlalchemy.pool import NullPool

    from . import db

    try:
        url = db.operational_url(hub)
        if url.startswith("sqlite") and not Path(url.split(":///", 1)[-1]).exists():
            return 0
        args = {} if url.startswith("sqlite") else {"connect_timeout": 5}
        engine = create_engine(url, poolclass=NullPool, connect_args=args, hide_parameters=True)
    except Exception:  # noqa: BLE001 - misconfigured or no driver: reported when the bridge starts
        return 0
    try:
        if not inspect(engine).has_table("hz_users"):
            return 0
        with engine.connect() as conn:
            return people(conn)
    except Exception:  # noqa: BLE001 - unreachable store: the bridge reports it
        return 0
    finally:
        engine.dispose()


def people(conn) -> int:
    """Accounts in hz_users that someone can sign in to. The local owner of
    sign-in-off mode (admin@localhost, any localhost address) is not a migrated
    account: a hub or gateway that ran locally still has its Open WebUI people
    to move."""
    from sqlalchemy import text

    return int(conn.execute(text(
        "SELECT COUNT(*) FROM hz_users WHERE lower(email) NOT LIKE '%@localhost' "
        "AND lower(email) NOT LIKE '%.localhost'")).scalar() or 0)
