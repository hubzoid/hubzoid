# Hubzoid Enterprise · access management. Production use requires a license
# with the "access" entitlement; free to run for development. See LICENSING.md.
"""Resolve a user's Open WebUI group names from OWUI's own database.

Open WebUI forwards the logged-in user's email to the bridge as
`X-OpenWebUI-User-Email` (when `ENABLE_FORWARD_USER_INFO_HEADERS` is on, which
hubzoid sets when it launches OWUI), but it does not forward group membership.
So the bridge reads the email and looks up that user's groups in OWUI's SQLite
DB, where the admin manages them on the Groups screen. This is what makes
"add a person to the `ornate` group in Open WebUI" actually grant the `ornate`
permission, with no separate proxy and no logout: the next request re-reads.

Read-only and fail-closed: any error (no DB, schema drift, a locked file)
yields no groups, so a lookup failure denies rather than grants.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

from .identity import normalize

log = logging.getLogger("hubzoid.access")

# OWUI 0.9.x: membership lives in the group_member association table, joining
# the group and user tables. Group/user are reserved words, hence the quoting.
_QUERY = """
SELECT g.name
FROM "group" g
JOIN group_member gm ON gm.group_id = g.id
JOIN "user" u ON u.id = gm.user_id
WHERE u.email = ?
"""


def _db_path(hub_dir: Path) -> Path:
    override = os.environ.get("HUBZOID_OWUI_DB")
    if override:
        return Path(override)
    return Path(hub_dir) / ".openwebui-data" / "webui.db"


def resolve_groups(hub_dir, email: str | None) -> set[str]:
    """Return the normalized OWUI group names the given user belongs to.

    Empty set when there is no email, no OWUI database, or anything goes wrong.
    Opened read-only so it never contends with OWUI's own writes.
    """
    if not email:
        return set()
    db = _db_path(Path(hub_dir))
    if not db.is_file():
        return set()
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
        try:
            rows = con.execute(_QUERY, (email,)).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        log.warning("OWUI group lookup failed for %r", email, exc_info=True)
        return set()
    return {normalize(r[0]) for r in rows if r and r[0]}
