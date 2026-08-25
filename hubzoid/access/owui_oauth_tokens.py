# Hubzoid access management. MIT licensed like the rest of the repository.
"""Read + decrypt a user's per-tool OAuth token from Open WebUI's database.

When a user connects an MCP tool in OWUI (``+ -> Integrations -> Tools``, an
OAuth 2.1 browser redirect), OWUI stores that user's token in the
``oauth_session`` table, provider ``mcp:<server_id>``, column ``token`` a
Fernet-encrypted JSON blob (``access_token``, ``refresh_token``,
``expires_at``, ...).

OWUI executes MCP tools inside its own loop and never forwards that token to
the model backend. But a Hubzoid model runs the agent loop itself, so for the
hub's agent to call the MCP server *as that user* it must read the token here
and inject it. We replicate OWUI's key derivation and Fernet decrypt exactly
(verified against open_webui/models/oauth_sessions.py in 0.11.0), keyed on the
shared ``WEBUI_SECRET_KEY`` OWUI and the bridge already both hold.

Read-only and fail-closed: any missing DB, key, row, or decrypt failure yields
None, so a lookup failure denies the tool rather than leaking or crashing.
The verified shape lives in [[owui-db-oauth-token-read]] alongside the groups
and api-key readers.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import time
from pathlib import Path

from . import owui_db

log = logging.getLogger("hubzoid.access")

# OWUI stores an MCP tool token under provider "mcp:<server_id>" (login/SSO
# tokens share the table under the bare provider name, e.g. "google").
_MCP_PREFIX = "mcp:"

# Fernet objects are cheap to reuse and re-deriving hashes per request is
# wasteful; cache by the raw secret string.
_fernet_cache: dict[str, object] = {}


def mcp_provider(server_id: str) -> str:
    """The ``oauth_session.provider`` value OWUI uses for an MCP server."""
    return f"{_MCP_PREFIX}{server_id}"


def _secret(hub_dir, *, primary: str = "OAUTH_SESSION_TOKEN_ENCRYPTION_KEY") -> str | None:
    """The key OWUI encrypts a blob with.

    ``primary`` is the blob-specific override env var OWUI checks first
    (``OAUTH_SESSION_TOKEN_ENCRYPTION_KEY`` for session tokens,
    ``OAUTH_CLIENT_INFO_ENCRYPTION_KEY`` for client info); both fall back to
    ``WEBUI_SECRET_KEY``. The ``.webui_secret_key`` file is the last resort so a
    bridge started without the env var still resolves it.
    """
    for var in (primary, "WEBUI_SECRET_KEY"):
        val = os.environ.get(var)
        if val:
            return val
    key_file = Path(hub_dir) / ".webui_secret_key"
    try:
        text = key_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def _fernet(secret: str):
    """A Fernet built from ``secret`` using OWUI's exact derivation.

    OWUI (oauth_sessions.py): a 44-char secret is already a urlsafe-b64 Fernet
    key and is used verbatim; anything else is SHA-256'd then urlsafe-b64
    encoded. Getting this wrong yields an InvalidToken on decrypt, not silent
    garbage, so a mismatch fails closed.
    """
    cached = _fernet_cache.get(secret)
    if cached is not None:
        return cached
    from cryptography.fernet import Fernet

    if len(secret) != 44:
        key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    else:
        key = secret.encode()
    f = Fernet(key)
    _fernet_cache[secret] = f
    return f


def resolve_user_id(hub_dir, email: str | None) -> str | None:
    """Map an OWUI user email to their ``user.id`` (what oauth_session keys on).

    ``oauth_session.user_id`` is OWUI's user id, not the email. The bridge
    resolves the caller by email (the forwarded ``X-OpenWebUI-User-Email``), so
    translate here. Case-insensitive to match OWUI's lowercased signups. None
    when there is no email, no DB, or no such user.
    """
    if not email:
        return None
    con = owui_db.connect_ro(hub_dir)
    if con is None:
        return None
    try:
        row = con.execute(
            'SELECT id FROM "user" WHERE lower(email) = lower(?) LIMIT 1', (email,)
        ).fetchone()
    except Exception:  # noqa: BLE001 — any schema drift denies, never crashes chat
        log.warning("OWUI user-id lookup failed for %r", email, exc_info=True)
        return None
    finally:
        con.close()
    return row[0] if row and row[0] else None


def read_token(hub_dir, user_id: str, server_id: str) -> dict | None:
    """This user's decrypted OAuth token dict for MCP ``server_id``, or None.

    Returns the full token dict OWUI stored (``access_token``,
    ``refresh_token``, ``expires_at``, ``scope``, ...). None when not connected,
    or when anything fails: no DB, no row, wrong/rotated secret, corrupt cell.
    Newest session wins (``created_at`` desc), matching OWUI's own lookup.
    """
    if not user_id or not server_id:
        return None
    secret = _secret(hub_dir)
    if not secret:
        log.warning("no WEBUI_SECRET_KEY available; cannot decrypt OWUI tokens")
        return None
    con = owui_db.connect_ro(hub_dir)
    if con is None:
        return None
    try:
        row = con.execute(
            "SELECT token FROM oauth_session "
            "WHERE user_id = ? AND provider = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (user_id, mcp_provider(server_id)),
        ).fetchone()
    except Exception:  # noqa: BLE001
        log.warning("OWUI token lookup failed (server %r)", server_id, exc_info=True)
        return None
    finally:
        con.close()
    if not row or not row[0]:
        return None
    try:
        f = _fernet(secret)
        decrypted = f.decrypt(row[0].encode()).decode()
        token = json.loads(decrypted)
    except Exception:  # noqa: BLE001 — bad key / corrupt cell => deny
        log.warning("OWUI token decrypt failed (server %r)", server_id, exc_info=True)
        return None
    return token if isinstance(token, dict) else None


def read_session(hub_dir, user_id: str, server_id: str) -> dict | None:
    """The user's newest MCP OAuth session for ``server_id``: ``{"id", "token"}``
    where ``token`` is the decrypted dict. Like :func:`read_token` but also
    returns the row ``id`` so a refreshed token can be written back to the same
    row. None when not connected or on any failure (fail-closed)."""
    if not user_id or not server_id:
        return None
    secret = _secret(hub_dir)
    if not secret:
        return None
    con = owui_db.connect_ro(hub_dir)
    if con is None:
        return None
    try:
        row = con.execute(
            "SELECT id, token FROM oauth_session "
            "WHERE user_id = ? AND provider = ? ORDER BY created_at DESC LIMIT 1",
            (user_id, mcp_provider(server_id)),
        ).fetchone()
    except Exception:  # noqa: BLE001
        log.warning("OWUI session read failed (server %r)", server_id, exc_info=True)
        return None
    finally:
        con.close()
    if not row or not row[1]:
        return None
    try:
        token = json.loads(_fernet(secret).decrypt(row[1].encode()).decode())
    except Exception:  # noqa: BLE001
        log.warning("OWUI session decrypt failed (server %r)", server_id, exc_info=True)
        return None
    return {"id": row[0], "token": token} if isinstance(token, dict) else None


def write_session(hub_dir, session_id: str, token: dict) -> bool:
    """Encrypt ``token`` and write it back to its ``oauth_session`` row, matching
    OWUI's own format so OWUI can still read it. Used only by the refresh path
    (OWUI never refreshes these for a Hubzoid model). Best-effort: returns False
    on any failure, never raises."""
    if not session_id or not isinstance(token, dict):
        return False
    secret = _secret(hub_dir)
    if not secret:
        return False
    try:
        enc = _fernet(secret).encrypt(json.dumps(token).encode()).decode()
    except Exception:  # noqa: BLE001
        log.warning("OWUI session encrypt failed", exc_info=True)
        return False
    now = int(time.time())
    exp = token.get("expires_at")
    con = owui_db.connect_rw(hub_dir)
    if con is None:
        return False
    try:
        con.execute(
            "UPDATE oauth_session SET token = ?, expires_at = ?, updated_at = ? WHERE id = ?",
            (enc, int(exp) if exp else now + 3600, now, session_id),
        )
        con.commit()
        return True
    except Exception:  # noqa: BLE001
        log.warning("OWUI session write-back failed", exc_info=True)
        return False
    finally:
        con.close()


def connected_server_ids(hub_dir, user_id: str) -> set[str]:
    """The MCP ``server_id``s this user has an OAuth session for.

    Cheap existence probe (no decrypt) so the runtime can decide which servers
    to even attempt to inject for this caller. Empty set on any failure.
    """
    if not user_id:
        return set()
    con = owui_db.connect_ro(hub_dir)
    if con is None:
        return set()
    try:
        rows = con.execute(
            "SELECT DISTINCT provider FROM oauth_session "
            "WHERE user_id = ? AND provider LIKE ?",
            (user_id, f"{_MCP_PREFIX}%"),
        ).fetchall()
    except Exception:  # noqa: BLE001
        log.warning("OWUI connected-servers lookup failed", exc_info=True)
        return set()
    finally:
        con.close()
    return {
        r[0][len(_MCP_PREFIX):]
        for r in rows
        if r and r[0] and r[0].startswith(_MCP_PREFIX)
    }
