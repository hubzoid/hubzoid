# Hubzoid MCP access management. MIT licensed like the rest of the repository.
"""Resolve an Open WebUI per-user API key to the owning user's email.

This is the credential story for the hosted MCP surface: a user mints an
API key in Open WebUI (Settings -> Account -> API keys) and presents it as
the Bearer token on ``/mcp``. We validate it with a read-only lookup against
OWUI's own database — the same database `owui_groups` reads — so identity,
revocation (delete the key in OWUI) and expiry all stay in the one place the
admin already manages.

OWUI itself never accepts these keys for anything: hubzoid launches OWUI
with API-key endpoint restrictions set to deny-all (see `webui.py`), so the
key is an identity credential for the MCP surface only.

Deliberately NOT accepted here: `BRIDGE_API_KEYS`. The bridge key is
infrastructure trust (it lets Open WebUI assert arbitrary user identities
via headers); an MCP caller must never inherit that power, so the two key
spaces are disjoint by construction — this module only ever matches rows of
OWUI's `api_key` table.
"""
from __future__ import annotations

import hmac
import logging
import sqlite3
import time
from pathlib import Path

from .owui_groups import _db_path

log = logging.getLogger("hubzoid.access")

# OWUI 0.9.x: per-user keys live in the api_key table (older releases kept a
# single key on the user row — those releases also cannot mint keys from the
# UI hubzoid ships, so we only support the table shape). Reserved word "key"
# is quoted.
_QUERY = """
SELECT k."key", k.expires_at, u.email
FROM api_key k
JOIN "user" u ON u.id = k.user_id
"""


def _is_expired(expires_at, now: float) -> bool:
    """True when `expires_at` is set and in the past.

    OWUI stores epoch seconds; be lenient and treat values that can only be
    milliseconds (> ~year 33658) as such rather than misreading them as far
    future.
    """
    if expires_at is None:
        return False
    try:
        ts = float(expires_at)
    except (TypeError, ValueError):
        return True  # unparseable expiry -> fail closed
    if ts > 1e12:
        ts /= 1000.0
    return ts < now


def resolve_email(hub_dir: Path, token: str | None) -> str | None:
    """Return the email of the OWUI user owning API key `token`, else None.

    Fail-closed: no token, no database, closed database, unknown key, or an
    expired key all resolve to None. The scan compares every row with a
    constant-time digest compare and never breaks early, so timing does not
    reveal *which* row matched (it does scale with the table size, i.e. the
    number of minted keys — O(keys) per request, fine at hub scale).
    """
    token = (token or "").strip()
    if not token:
        return None
    db = _db_path(Path(hub_dir))
    if not db.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
        try:
            rows = con.execute(_QUERY).fetchall()
        finally:
            con.close()
    except sqlite3.Error as exc:
        log.warning("api-key lookup failed (%s); denying", exc)
        return None

    now = time.time()
    match: str | None = None
    for key, expires_at, email in rows:
        ok = hmac.compare_digest(str(key or ""), token)
        if ok and not _is_expired(expires_at, now) and email:
            match = str(email)
    return match
