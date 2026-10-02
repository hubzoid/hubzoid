"""The personal MCP servers the caller may reach this turn (default UI mode).

Same return shape and rules as ``hubzoid.owui_mcp.per_user_servers``, which
dispatches here unless the deployment runs Open WebUI mode, so all
three runtimes use it unchanged:

  * nobody for an anonymous caller, or on a surface that may not carry
    personal credentials (``HUBZOID_RESTRICTED_SURFACES``, the restricted-tool
    rule), or for a blocked person;
  * only connectors the caller connected, that are switched on, offered in
    this agent, and whose ``connector_<id>`` capability the caller holds;
  * the administrator's tool allow-list;
  * never a server whose key would replace one of the hub's own MCP servers;
  * each with a currently valid token (refreshed if due). A connection that
    cannot be used now is dropped for the turn; the person reconnects.
"""
from __future__ import annotations

import logging
from pathlib import Path

from . import capability, registry, tokens

log = logging.getLogger("hubzoid.connectors")

KEY_PREFIX = "my_"


def server_key(connector_id: str) -> str:
    """The MCP server key a connector is known by in a turn (tools surface as
    ``mcp__my_<id>__<tool>`` in Claude). Prefixed so a personal server never
    takes the name of a hub MCP server of the same app."""
    return f"{KEY_PREFIX}{connector_id}"


def _blocked(hub_dir, email: str) -> bool:
    from ..access import store_for

    try:
        return store_for(Path(hub_dir)).is_suspended(email)
    except Exception:  # noqa: BLE001 — cannot tell: refuse
        log.warning("connectors: access store unavailable; no personal servers this turn")
        return True


def allowed_ids(hub_dir, email: str, ids) -> set[str]:
    """Connector ids ``email`` may use somewhere in this deployment: those
    offered in an agent where the person holds their capability. Empty for a
    blocked person or when access cannot be checked."""
    from .. import deployment
    from ..access import store_for
    from ..access.identity import normalize

    ids = set(ids)
    email = normalize(email)
    if not ids or not email:
        return set()
    try:
        gs = store_for(Path(hub_dir))
        if gs.is_suspended(email):
            return set()
        try:
            hubs = [h["key"] for h in deployment.hubs(Path(hub_dir))]
        except Exception:  # noqa: BLE001 — a standalone hub without a readable manifest
            hubs = [normalize(Path(hub_dir).name)]
        out: set[str] = set()
        for hub in hubs:
            offered = registry.offered_in(hub_dir, hub)
            out |= {i for i in ids if i in offered and gs.can(email, hub, capability(i))}
        return out
    except Exception:  # noqa: BLE001 — fail closed
        log.warning("connectors: access check failed", exc_info=True)
        return set()


def per_user_servers(hub_dir, identity=None, *, reserved: set[str] | None = None, **_kw) -> list:
    """``[owui_mcp.PerUserServer]`` for ``identity`` (default: the current
    request's). Empty on every refusal path; never raises for missing data."""
    from ..access.guard import allowed_surfaces
    from ..access.identity import current_identity
    from ..owui_mcp import PerUserServer, _connector_gate, _hub_server_keys

    if identity is None:
        identity = current_identity()
    if identity is None or getattr(identity, "is_anonymous", True):
        return []
    if getattr(identity, "surface", "") not in allowed_surfaces():
        return []
    try:
        user_id = tokens.user_id_for(hub_dir, identity.user)
        connections = tokens.for_user(hub_dir, user_id) if user_id else []
        connections = [c for c in connections if c.status != "expired"]
        if not connections:
            return []
        # Only the connectors this agent offers, whatever the person holds elsewhere.
        offered = registry.offered_in(hub_dir, Path(hub_dir).name)
        by_id = {c.id: c for c in registry.list_all(hub_dir) if c.enabled and c.id in offered}
    except Exception:  # noqa: BLE001 — a store problem must never break chat
        log.warning("connectors: personal connections unavailable this turn", exc_info=True)
        return []
    connections = [c for c in connections if c.connector_id in by_id]
    if not connections or _blocked(hub_dir, identity.user):
        return []
    permitted = _connector_gate(hub_dir, identity)

    reserved = _hub_server_keys(hub_dir) if reserved is None else set(reserved)
    out: list = []
    for conn in sorted(connections, key=lambda c: c.connector_id):
        connector = by_id[conn.connector_id]
        if not permitted(connector.id):
            log.info("connectors: %s lacks %s; %s not offered", identity.user,
                     capability(connector.id), connector.id)
            continue
        key = server_key(connector.id)
        if key in reserved:
            log.warning("connectors: personal server %r would replace the hub's %r MCP server; "
                        "skipped this turn", connector.id, key)
            continue
        if connector.auth_type == "none":
            record = tokens.token_record(hub_dir, user_id, connector.id)
            if not record or record.get("url") not in (None, connector.url):
                continue
            headers: dict = {}
        else:
            try:
                access = tokens.access_token_for(hub_dir, user_id, connector.id, url=connector.url)
            except Exception:  # noqa: BLE001 — one connection never sinks the turn
                log.warning("connectors: %s token unavailable this turn", connector.id,
                            exc_info=True)
                access = None
            if not access:
                continue
            headers = {"Authorization": f"Bearer {access}"}
        out.append(PerUserServer(
            key=key, url=connector.url, headers=headers,
            allowed_tools=tuple(connector.tool_allowlist) if connector.tool_allowlist else None,
            server_id=connector.id, app=connector.id,
        ))
    if out:
        log.info("connectors: %d personal server(s) for %s", len(out), identity.user)
    return out
