# Hubzoid access management. MIT licensed like the rest of the repository.
"""Recover the OAuth client credentials needed to refresh a token.

OWUI stores the registered client (dynamic or static) inside the tool-server
connection as ``info.oauth_client_info`` - a Fernet-encrypted JSON blob keyed by
``OAUTH_CLIENT_INFO_ENCRYPTION_KEY`` (default ``WEBUI_SECRET_KEY``, the same
scheme as the session tokens). For ``oauth_2.1_static`` OWUI overlays the
admin-entered ``info.oauth_client_id`` / ``oauth_client_secret``.

We decrypt it and return just what a refresh POST needs: the token endpoint,
client id/secret, and scope/resource. Read-only, fail-closed: None on anything
missing. Mirrors OWUI's ``resolve_oauth_client_info``.
"""
from __future__ import annotations

import json
import logging

from . import owui_oauth_tokens as tokens
from . import owui_tool_servers as servers

log = logging.getLogger("hubzoid.access")

_CLIENT_INFO_KEY = "OAUTH_CLIENT_INFO_ENCRYPTION_KEY"


def _decrypt(hub_dir, blob: str) -> dict | None:
    secret = tokens._secret(hub_dir, primary=_CLIENT_INFO_KEY)
    if not secret or not blob:
        return None
    try:
        data = json.loads(tokens._fernet(secret).decrypt(blob.encode()).decode())
    except Exception:  # noqa: BLE001
        log.warning("OWUI oauth_client_info decrypt failed", exc_info=True)
        return None
    return data if isinstance(data, dict) else None


def client_info(hub_dir, server_id: str, conn: dict | None = None) -> dict | None:
    """Refresh parameters for ``server_id`` - ``{token_endpoint, client_id,
    client_secret, scope, resource}`` - or None if unavailable.

    ``conn`` is the raw connection dict when the caller already has it; else it
    is looked up. Includes OWUI's static-credential overlay.
    """
    if conn is None:
        conn = servers.raw_mcp_connection(hub_dir, server_id)
    if not conn:
        return None
    info = conn.get("info") or {}
    data = _decrypt(hub_dir, info.get("oauth_client_info") or "") or {}
    if (conn.get("auth_type") or "").lower() == "oauth_2.1_static":
        if info.get("oauth_client_id"):
            data["client_id"] = info["oauth_client_id"]
        if info.get("oauth_client_secret"):
            data["client_secret"] = info["oauth_client_secret"]
    client_id = data.get("client_id")
    token_endpoint = (data.get("server_metadata") or {}).get("token_endpoint")
    if not client_id or not token_endpoint:
        return None
    return {
        "token_endpoint": token_endpoint,
        "client_id": client_id,
        "client_secret": data.get("client_secret"),
        "scope": data.get("scope") or info.get("oauth_scope"),
        "resource": data.get("resource"),
    }
