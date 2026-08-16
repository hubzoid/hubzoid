# Hubzoid Enterprise · access management. Production use requires a license
# with the "access" entitlement; free to run for development. See LICENSING.md.
"""One place Hubzoid touches Open WebUI's database.

Every read of OWUI's own tables (groups, API keys, per-user OAuth tokens)
goes through here so there is a single accessor to point at a different
store later. Today that store is OWUI's SQLite file (`webui.db`); a move to
a shared Postgres is a change in this one module, not a scatter of
`sqlite3.connect` calls across the package.

Read-only and fail-closed by construction: `connect_ro` opens the file in
SQLite read-only mode (`?mode=ro`) so a Hubzoid read never contends with
OWUI's own writes, and returns None (rather than raising) when the DB is
absent or unopenable, so every caller degrades to "no data" — which, for
the access layer, means deny.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path


def db_path(hub_dir) -> Path:
    """Path to OWUI's database for this hub.

    `HUBZOID_OWUI_DB` overrides (tests, or a relocated data dir); otherwise
    the canonical `<hub>/.openwebui-data/webui.db` OWUI is launched against.
    """
    override = os.environ.get("HUBZOID_OWUI_DB")
    if override:
        return Path(override)
    return Path(hub_dir) / ".openwebui-data" / "webui.db"


def connect_ro(hub_dir) -> sqlite3.Connection | None:
    """A read-only connection to OWUI's DB, or None if it cannot be opened.

    Callers must close what they get. A None return is the fail-closed
    signal: no DB, a locked file, or a bad path all resolve to "no data".
    """
    db = db_path(Path(hub_dir))
    if not db.is_file():
        return None
    try:
        return sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
    except sqlite3.Error:
        return None
