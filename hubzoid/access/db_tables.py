# Hubzoid access management. MIT licensed like the rest of the repository.
"""Schema for the access store's tables in the one hub-owned database.

All tables are `hz_`-prefixed (we never touch Open WebUI's schema). Created
idempotently on first use, across SQLite and Postgres, via SQLAlchemy Core — the
same thin, ORM-free path as `db.py`.
"""
from __future__ import annotations

import threading

from sqlalchemy import text
from sqlalchemy.engine import Engine

_done: set[int] = set()
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
        created  REAL
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
]


def ensure_access_tables(engine: Engine) -> None:
    """Create the access tables (idempotent, once per engine per process)."""
    key = id(engine)
    if key in _done:
        return
    with _lock:
        if key in _done:
            return
        with engine.begin() as conn:
            for stmt in _DDL:
                conn.execute(text(stmt))
            # Seed the single revision row if missing.
            row = conn.execute(
                text("SELECT 1 FROM hz_policy_revision WHERE id=1")
            ).fetchone()
            if not row:
                conn.execute(
                    text("INSERT INTO hz_policy_revision (id, rev) VALUES (1, 0)")
                )
        _done.add(key)
