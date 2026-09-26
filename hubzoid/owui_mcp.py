"""Per-user MCP servers from Open WebUI's native connections.

OWUI lets each user connect an MCP tool for themselves (``+ -> Integrations
-> Tools``, an OAuth 2.1 redirect) and stores that user's token. OWUI would
run the tool in its own loop, but a Hubzoid model runs the agent loop itself,
so OWUI never gets the chance. This module closes that gap: for the caller of
the current turn it finds which OWUI MCP servers they connected, looks up each
server's URL, reads and decrypts *their* token, and describes each server with
the caller's own ``Authorization: Bearer``.

This is the single per-turn source for all three runtimes. It returns plain
data (:class:`PerUserServer`), and each runtime adapter builds its own client
from it inside the turn:

  * Claude (``factory_claude.ClaudeRuntime``) merges :func:`per_user_specs`,
    the Claude-SDK ``http`` specs, into its per-turn options.
  * OpenAI Agents (``runtime.OpenAIAgentsRuntime``) connects a Streamable HTTP
    client per server and runs a per-turn clone of the agent.
  * Codex (``factory_codex.CodexRuntime``) connects the same clients and adds
    their tools to a per-turn copy of its registry.

So two people on the same hub each reach the same MCP server as themselves and
see only their own data, whichever backend the hub runs on.

Entirely no-op (empty result) when the switch ``OWUI_NATIVE_MCP`` is not set,
the caller is anonymous, the caller's surface may not carry personal tokens
(``HUBZOID_RESTRICTED_SURFACES``, the restricted-tool rule), or they have
connected nothing. On a hub whose access is managed in the Console, each server
also needs the caller's ``connector_<app>`` capability (``app`` is the server's
id, see :func:`app_key`). Legacy hubs keep today's behaviour apart from the
surface rule. A server whose key would replace a hub MCP server is skipped for
that turn, so a personal server never shadows a hub tool.

Token freshness (expiry + refresh) is delegated to ``owui_refresh``, so a server
is described only with a currently valid token. An expired-and-unrefreshable one
is dropped for that turn (the user reconnects).
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

from . import owui_refresh as refresh
from .access import owui_oauth_tokens as tokens
from .access import owui_tool_servers as servers

log = logging.getLogger("hubzoid.owui_mcp")

_NAME_RE = re.compile(r"[^a-z0-9]+")
_APP_RE = re.compile(r"[^a-z0-9_]+")

_TRUTHY = {"1", "true", "yes", "on"}

CONNECTOR_PREFIX = "connector_"

# The Claude runtime's own in-process server key (factory_claude._MCP_NAMESPACE).
_HUB_SERVER_KEY = "hubzoid"


def enabled() -> bool:
    """On only when the operator set ``OWUI_NATIVE_MCP=true`` - the same one
    switch that configures OWUI in ``webui.py``. Opt-in, so hubs that do not use
    native MCP pay nothing (no per-turn DB reads) and stay env-authoritative."""
    return os.environ.get("OWUI_NATIVE_MCP", "").strip().lower() in _TRUTHY


def app_key(name: str) -> str:
    """The app a connector is known by: lowercase ``[a-z0-9_]``.

    For an Open WebUI MCP server it is derived from the server's ``info.id``
    (the ID the admin typed when registering it), so a server registered as
    ``gmail`` is the app ``gmail`` and the capability ``connector_gmail``.
    """
    return _APP_RE.sub("_", (name or "").strip().lower()).strip("_")


def capability(app: str) -> str:
    """The capability that gates a connector: ``connector_<app>``."""
    return f"{CONNECTOR_PREFIX}{app_key(app)}"


@dataclass(frozen=True)
class PerUserServer:
    """One MCP server this caller may reach this turn, as themselves.

    Runtime-neutral: ``headers`` carries the caller's Bearer, and
    ``allowed_tools`` is the admin's tool allow-list (bare MCP tool names) or
    None for no filter. Never log or return ``headers``.
    """

    key: str
    url: str
    headers: dict = field(repr=False)
    allowed_tools: tuple[str, ...] | None
    server_id: str
    app: str


def _namespace(name: str, server_id: str, taken: set[str]) -> str:
    """A safe, stable MCP server key -> tools surface as mcp__<key>__<tool>.

    Sanitized to ``owui_<name>``; on a collision (two servers with the same
    display name) a short slice of the unique server_id disambiguates so both
    stay reachable.
    """
    base = "owui_" + (_NAME_RE.sub("_", (name or "").lower()).strip("_") or "mcp")
    key = base
    if key in taken:
        key = f"{base}_{_NAME_RE.sub('', server_id.lower())[:8]}"
    taken.add(key)
    return key


def _hub_server_keys(hub_dir) -> set[str]:
    """Keys the hub itself already uses for MCP servers (``connectors/.mcp.json``
    plus the auto-injected browser and the Claude runtime's own server)."""
    keys = {_HUB_SERVER_KEY}
    try:
        from .loaders import mcp as mcp_loader

        keys |= set(mcp_loader.load_all_raw(hub_dir))
    except Exception:  # noqa: BLE001 — an unreadable config is the build's problem
        log.debug("owui-mcp: hub MCP config unreadable; reserving only %r", _HUB_SERVER_KEY)
    return keys


def _connector_gate(hub_dir, identity):
    """A per-server check for this caller, or None to refuse every server.

    Managed hub (Console-authoritative): each server needs ``connector_<app>``
    through ``guard.decide``. Legacy hub: no extra check. Fails closed when the
    access store cannot say which kind the hub is.
    """
    from pathlib import Path

    from .access import store_for
    from .access.guard import decide

    hub = Path(hub_dir).name
    try:
        managed = store_for(hub_dir).is_authoritative(hub)
    except Exception:  # noqa: BLE001
        log.warning("owui-mcp: access store unavailable; no personal servers this turn")
        return None
    if not managed:
        return lambda app: True
    return lambda app: decide(hub_dir, identity, capability(app))[0]


def per_user_servers(hub_dir, identity, *, reserved: set[str] | None = None) -> list[PerUserServer]:
    """The MCP servers this caller connected and may use this turn.

    ``reserved`` are server keys that must not be replaced (defaults to the
    hub's own MCP server keys). Empty on every refusal path, never raises for
    a missing DB, key or row.
    """
    if not enabled() or identity is None or getattr(identity, "is_anonymous", True):
        return []
    # A personal token follows the same surface rule as restricted tools: a
    # shared Slack channel or bot-token surface must never carry it.
    from .access.guard import allowed_surfaces
    if getattr(identity, "surface", "") not in allowed_surfaces():
        return []

    user_id = tokens.resolve_user_id(hub_dir, identity.user)
    if not user_id:
        return []
    connected = tokens.connected_server_ids(hub_dir, user_id)
    if not connected:
        return []
    permitted = _connector_gate(hub_dir, identity)
    if permitted is None:
        return []

    reserved = _hub_server_keys(hub_dir) if reserved is None else set(reserved)
    by_id = {c["id"]: c for c in servers.list_mcp_connections(hub_dir)}
    out: list[PerUserServer] = []
    taken: set[str] = set()
    for server_id in sorted(connected):
        conn = by_id.get(server_id)
        if conn is None:
            # Connected once, server since removed by the admin. Skip quietly.
            continue
        app = app_key(server_id)
        if not permitted(app):
            log.info("owui-mcp: %s lacks %s; server %r not injected",
                     identity.user, capability(app), server_id)
            continue
        key = _namespace(conn["name"], server_id, taken)
        if key in reserved:
            log.warning("owui-mcp: personal server %r would replace the hub's %r "
                        "MCP server; skipped this turn", server_id, key)
            continue
        # Valid token, refreshing if it has expired. None => connected but not
        # usable now (refresh failed / none); drop it this turn (owui_refresh
        # logged why) and the user reconnects.
        access_token = refresh.access_token_for(hub_dir, user_id, server_id)
        if not access_token:
            continue
        allow = conn.get("allowed_tools")
        out.append(PerUserServer(
            key=key, url=conn["url"],
            headers={"Authorization": f"Bearer {access_token}"},
            allowed_tools=tuple(allow) if allow else None,
            server_id=server_id, app=app,
        ))

    if out:
        log.info("owui-mcp: %d personal server(s) for %s", len(out), identity.user)
    return out


def per_user_specs(hub_dir, identity, *, reserved: set[str] | None = None) -> tuple[dict, list[str]]:
    """``({server_key: http_mcp_spec}, [allowed_tool_globs])`` for this caller.

    The Claude-SDK view of :func:`per_user_servers`. Each spec is a
    ``McpHttpServerConfig`` with the caller's Bearer; the globs honor the
    admin's per-server tool allow-list when one is set, else ``*``.
    """
    specs: dict[str, dict] = {}
    allowed: list[str] = []
    for srv in per_user_servers(hub_dir, identity, reserved=reserved):
        specs[srv.key] = {"type": "http", "url": srv.url, "headers": dict(srv.headers)}
        if srv.allowed_tools:
            allowed.extend(f"mcp__{srv.key}__{t}" for t in srv.allowed_tools)
        else:
            allowed.append(f"mcp__{srv.key}__*")
    return specs, allowed
