"""Versioned schema for Hubzoid's own tables, run with Alembic.

Two stores, two migration sets, each with its own version table so they never
collide with each other, with Open WebUI's migrations or with DBOS's:

  * ``operational`` (``hz_alembic_operational``): the shared deployment store:
    access, identities, audit, workflow catalog and state, usage.
  * ``hub`` (``hz_alembic_hub``): each hub's own database: inbound history.

In a standalone hub both live in the same SQLite file. ``upgrade()`` runs once per
engine per process, under a cross-process lock (a Postgres advisory lock, or a
file lock next to a SQLite database) so bridges starting together don't race.

Existing installs predate versioning and may hold any subset of today's tables.
The baseline revisions therefore create only what is missing and then verify the
columns of what already exists; a table that doesn't match is refused rather
than stamped. A database stamped by a newer Hubzoid is refused too.
"""
from __future__ import annotations

import contextlib
import logging
import threading
from pathlib import Path
from weakref import WeakKeyDictionary

from sqlalchemy import inspect
from sqlalchemy.engine import Engine

log = logging.getLogger("hubzoid.migrations")

STORES = {
    "operational": "hz_alembic_operational",
    "hub": "hz_alembic_hub",
}
_ROOT = Path(__file__).parent
_done: "WeakKeyDictionary[Engine, set[str]]" = WeakKeyDictionary()
_lock = threading.Lock()


class SchemaError(RuntimeError):
    """The database schema can't be used by this version of Hubzoid."""


def _script(store: str):
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config()
    cfg.set_main_option("script_location", str(_ROOT / store))
    return cfg, ScriptDirectory.from_config(cfg)


@contextlib.contextmanager
def _cross_process_lock(engine: Engine, store: str):
    """Serialize migrations of one database across processes."""
    if engine.dialect.name == "postgresql":
        from sqlalchemy import text

        key = 726104900 + list(STORES).index(store)
        with engine.connect() as conn:
            conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": key})
            conn.commit()
            try:
                yield
            finally:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
                conn.commit()
        return
    db = engine.url.database
    if engine.dialect.name == "sqlite" and db and db != ":memory:":
        import fcntl

        lock_path = Path(db).with_name(Path(db).name + f".{store}.migrate.lock")
        with open(lock_path, "a") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
        return
    yield


def current(engine: Engine, store: str) -> str | None:
    """The revision this database is at for `store`, or None if unversioned."""
    table = STORES[store]
    if not inspect(engine).has_table(table):
        return None
    from sqlalchemy import text

    with engine.connect() as conn:
        return conn.execute(text(f"SELECT version_num FROM {table}")).scalar()


def head(store: str) -> str:
    return _script(store)[1].get_current_head()


def upgrade(engine: Engine, store: str) -> None:
    """Bring `store`'s tables in this database to the latest revision."""
    if store not in STORES:
        raise ValueError(f"unknown store {store!r}")
    with _lock:
        if store in _done.get(engine, set()):
            return
        cfg, script = _script(store)
        with _cross_process_lock(engine, store):
            at = current(engine, store)
            if at is not None and at not in {r.revision for r in script.walk_revisions()}:
                raise SchemaError(
                    f"The {store} database is at schema revision {at}, which this "
                    "version of Hubzoid does not know. It was upgraded by a newer "
                    "Hubzoid; install that version (or restore a backup)."
                )
            _run(engine, cfg, script, store)
        _done.setdefault(engine, set()).add(store)


def _run(engine: Engine, cfg, script, store: str) -> None:
    from alembic.runtime.environment import EnvironmentContext

    def do_upgrade(rev, context):
        return script._upgrade_revs("head", rev)

    with EnvironmentContext(cfg, script, fn=do_upgrade, destination_rev="head") as env:
        with engine.connect() as conn:
            env.configure(connection=conn, version_table=STORES[store])
            with env.begin_transaction():
                env.run_migrations()
            conn.commit()
    log.info("migrations: %s store at %s", store, current(engine, store))


def verify_columns(conn, table: str, expected: set[str]) -> None:
    """Refuse a pre-existing table whose columns don't include `expected`."""
    insp = inspect(conn)
    if not insp.has_table(table):
        return
    have = {c["name"] for c in insp.get_columns(table)}
    missing = expected - have
    if missing:
        raise SchemaError(
            f"Existing table {table} is missing columns {sorted(missing)}; it was "
            "not created by a Hubzoid version this upgrade supports."
        )
