# Hubzoid access management. MIT licensed like the rest of the repository.
"""Schema for the access store's tables in the one hub-owned database.

All tables are `hz_`-prefixed (we never touch Open WebUI's schema). Created
idempotently on first use, across SQLite and Postgres, via SQLAlchemy Core — the
same thin, ORM-free path as `db.py`.
"""
from __future__ import annotations

import threading
from weakref import WeakSet

from sqlalchemy import text
from sqlalchemy.engine import Engine

_done: WeakSet[Engine] = WeakSet()
_lock = threading.Lock()

# hz_grants        : the policy rows — (subject, hub, permission). subject '*' =
#                    everyone; hub '*' = the org domain.
# hz_policy_revision: one-row monotonic counter for cross-process enforcer freshness.
# hz_identities    : one row per grantee; `subject` is the Casbin subject id, the
#                    stable key everything grants to. email/owui_id/phone are
#                    lookup columns filled at migration / first login / resolve.
# hz_identity_attrs: per-(hub, subject) attributes (e.g. center) for in-tool data
#                    scoping. Casbin never reads these.
_DDL = [
    """
    CREATE TABLE IF NOT EXISTS hz_grants (
        subject    TEXT NOT NULL,
        hub        TEXT NOT NULL,
        permission TEXT NOT NULL,
        PRIMARY KEY (subject, hub, permission)
    )
    """,
    "CREATE INDEX IF NOT EXISTS hz_grants_hub ON hz_grants (hub)",
    """
    CREATE TABLE IF NOT EXISTS hz_policy_revision (
        id  INTEGER PRIMARY KEY,
        rev INTEGER NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS hz_identities (
        subject  TEXT PRIMARY KEY,
        email    TEXT,
        owui_id  TEXT,
        phone    TEXT,
        display  TEXT,
        pending  INTEGER NOT NULL DEFAULT 0,
        created  DOUBLE PRECISION
    )
    """,
    "CREATE INDEX IF NOT EXISTS hz_identities_email ON hz_identities (email)",
    "CREATE INDEX IF NOT EXISTS hz_identities_owui ON hz_identities (owui_id)",
    "CREATE INDEX IF NOT EXISTS hz_identities_phone ON hz_identities (phone)",
    """
    CREATE TABLE IF NOT EXISTS hz_identity_attrs (
        hub     TEXT NOT NULL,
        subject TEXT NOT NULL,
        k       TEXT NOT NULL,
        v       TEXT,
        PRIMARY KEY (hub, subject, k)
    )
    """,
    # hz_meta: small key/value flags — notably `casbin_authoritative` (set at
    # migration cutover or fresh-install bootstrap) and `bootstrapped`.
    """
    CREATE TABLE IF NOT EXISTS hz_meta (
        k TEXT PRIMARY KEY,
        v TEXT
    )
    """,
    # hz_access_audit: an append-only trail of access CHANGES (grant/revoke),
    # written in the same transaction as the mutation. Complements the per-tool
    # decision log (access/audit.py); together they are the Audit screen.
    """
    CREATE TABLE IF NOT EXISTS hz_access_audit (
        ts         DOUBLE PRECISION NOT NULL,
        actor      TEXT,
        action     TEXT NOT NULL,
        subject    TEXT,
        hub        TEXT,
        permission TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS hz_access_audit_ts ON hz_access_audit (ts)",
    # hz_workflows: the shared workflow CATALOG. Each bridge publishes its hub's
    # workflows here at launch, so the org-wide portal (served by one bridge over
    # the shared DB) can list every hub's workflows, not just its own.
    """
    CREATE TABLE IF NOT EXISTS hz_workflows (
        hub      TEXT NOT NULL,
        name     TEXT NOT NULL,
        schedule TEXT,
        timezone TEXT,
        updated  DOUBLE PRECISION,
        PRIMARY KEY (hub, name)
    )
    """,
]


def ensure_access_tables(engine: Engine) -> None:
    """Create the access tables (idempotent, once per engine per process)."""
    key = engine
    if key in _done:
        return
    with _lock:
        if key in _done:
            return
        with engine.begin() as conn:
            if engine.dialect.name == 'postgresql':
                conn.execute(text("SELECT pg_advisory_xact_lock(726104812)"))
            for stmt in _DDL:
                conn.execute(text(stmt))
            if engine.dialect.name == 'postgresql':
                for table, column in (('hz_identities', 'created'), ('hz_access_audit', 'ts'), ('hz_workflows', 'updated')):
                    kind = conn.execute(text("SELECT data_type FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=:t AND column_name=:c"), {'t': table, 'c': column}).scalar()
                    if kind == 'real':
                        conn.execute(text(f'ALTER TABLE {table} ALTER COLUMN {column} TYPE DOUBLE PRECISION'))
            conn.execute(text("INSERT INTO hz_policy_revision (id, rev) VALUES (1, 0) ON CONFLICT (id) DO NOTHING"))
        _done.add(key)
