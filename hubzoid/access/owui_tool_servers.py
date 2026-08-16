# Hubzoid Enterprise · access management. Production use requires a license
# with the "access" entitlement; free to run for development. See LICENSING.md.
"""Read the admin-registered MCP tool servers from Open WebUI's config.

OWUI keeps every tool-server connection (OpenAPI and MCP) as one JSON list
under the ``tool_server.connections`` key of its per-key ``config`` table
(``key`` PK, ``value`` JSON). Each MCP entry carries the server URL, an
``info.id`` (the ``server_id`` used everywhere else), an ``auth_type``, and an
optional ``config.function_name_filter_list`` allow-list of tool names.

Hubzoid reads this to learn *where* a connected MCP server lives (its URL) and
*which* of its tools the admin permitted, so the agent can call it with the
caller's own token (read separately from ``oauth_session`` in
[[owui-db-oauth-token-read]]). Read-only, fail-closed: any failure yields an
empty list, so a missing or malformed config offers no servers rather than
crashing chat.
"""
from __future__ import annotations

import json
import logging

from . import owui_db

log = logging.getLogger("hubzoid.access")

_KEY = "tool_server.connections"


def list_mcp_connections(hub_dir) -> list[dict]:
    """The registered MCP servers, normalized to what the runtime needs.

    Each item: ``{id, name, url, auth_type, allowed_tools}`` where
    ``allowed_tools`` is the admin's tool-name allow-list or None (no filter).
    OpenAPI connections and malformed entries are skipped. Empty list on any
    failure.
    """
    con = owui_db.connect_ro(hub_dir)
    if con is None:
        return []
    try:
        row = con.execute("SELECT value FROM config WHERE key = ?", (_KEY,)).fetchone()
    except Exception:  # noqa: BLE001 — schema drift => no servers, never crash
        log.warning("OWUI tool-server config read failed", exc_info=True)
        return []
    finally:
        con.close()
    if not row or row[0] is None:
        return []
    raw = row[0]
    try:
        conns = raw if isinstance(raw, list) else json.loads(raw)
    except (ValueError, TypeError):
        log.warning("OWUI tool-server config was not valid JSON")
        return []
    if not isinstance(conns, list):
        return []

    out: list[dict] = []
    for c in conns:
        if not isinstance(c, dict):
            continue
        if (c.get("type") or "").lower() != "mcp":
            continue
        info = c.get("info") or {}
        server_id = info.get("id")
        url = c.get("url")
        if not server_id or not url:
            continue
        cfg = c.get("config") or {}
        filt = cfg.get("function_name_filter_list")
        out.append({
            "id": server_id,
            "name": info.get("name") or server_id,
            "url": url,
            "auth_type": (c.get("auth_type") or "").lower(),
            "allowed_tools": list(filt) if isinstance(filt, list) and filt else None,
        })
    return out


def mcp_connection(hub_dir, server_id: str) -> dict | None:
    """The one MCP connection whose ``info.id`` is ``server_id``, or None."""
    if not server_id:
        return None
    for c in list_mcp_connections(hub_dir):
        if c["id"] == server_id:
            return c
    return None
