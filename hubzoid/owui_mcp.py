"""Per-user MCP servers from Open WebUI's native connections.

OWUI lets each user connect an MCP tool for themselves (``+ -> Integrations
-> Tools``, an OAuth 2.1 redirect) and stores that user's token. OWUI would
run the tool in its own loop, but a Hubzoid model runs the agent loop itself,
so OWUI never gets the chance. This module closes that gap: for the caller of
the current turn it finds which OWUI MCP servers they connected, looks up each
server's URL, reads and decrypts *their* token, and emits Claude-SDK ``http``
MCP specs carrying the caller's own ``Authorization: Bearer``.

The runtime merges these per-turn (``factory_claude.ClaudeRuntime`` /
``runtime.OpenAIAgentsRuntime``), so two people on the same hub each reach the
same MCP server as themselves and see only their own data. Entirely no-op
(empty result) when the caller is anonymous, has connected nothing, or the
switch ``OWUI_NATIVE_MCP`` is not set. Token freshness (expiry + refresh) is
delegated to ``owui_refresh``, so a server is injected only with a currently
valid token; an expired-and-unrefreshable one is dropped for that turn (the
user reconnects in OWUI). See the design doc
``docs/per-user-tool-connections.html``.
"""
from __future__ import annotations

import logging
import os
import re

from . import owui_refresh as refresh
from .access import owui_oauth_tokens as tokens
from .access import owui_tool_servers as servers

log = logging.getLogger("hubzoid.owui_mcp")

_NAME_RE = re.compile(r"[^a-z0-9]+")

_TRUTHY = {"1", "true", "yes", "on"}


def enabled() -> bool:
    """On only when the operator set ``OWUI_NATIVE_MCP=true`` - the same one
    switch that configures OWUI in ``webui.py``. Opt-in, so hubs that do not use
    native MCP pay nothing (no per-turn DB reads) and stay env-authoritative."""
    return os.environ.get("OWUI_NATIVE_MCP", "").strip().lower() in _TRUTHY


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


def per_user_specs(hub_dir, identity) -> tuple[dict, list[str]]:
    """``({server_key: http_mcp_spec}, [allowed_tool_globs])`` for this caller.

    Empty when disabled, anonymous, the user is unknown to OWUI, or they have
    connected no server that is still registered with a usable token. Each spec
    is a Claude-SDK ``McpHttpServerConfig`` with the caller's Bearer; the globs
    honor the admin's per-server tool allow-list when one is set, else ``*``.
    """
    if not enabled() or identity is None or getattr(identity, "is_anonymous", True):
        return {}, []

    user_id = tokens.resolve_user_id(hub_dir, identity.user)
    if not user_id:
        return {}, []
    connected = tokens.connected_server_ids(hub_dir, user_id)
    if not connected:
        return {}, []

    by_id = {c["id"]: c for c in servers.list_mcp_connections(hub_dir)}
    specs: dict[str, dict] = {}
    allowed: list[str] = []
    taken: set[str] = set()
    for server_id in sorted(connected):
        conn = by_id.get(server_id)
        if conn is None:
            # Connected once, server since removed by the admin. Skip quietly.
            continue
        # Valid token, refreshing if it has expired. None => connected but not
        # usable now (refresh failed / none); drop it this turn (owui_refresh
        # logged why) and the user reconnects in OWUI.
        access_token = refresh.access_token_for(hub_dir, user_id, server_id)
        if not access_token:
            continue
        key = _namespace(conn["name"], server_id, taken)
        specs[key] = {
            "type": "http",
            "url": conn["url"],
            "headers": {"Authorization": f"Bearer {access_token}"},
        }
        allow = conn.get("allowed_tools")
        if allow:
            allowed.extend(f"mcp__{key}__{t}" for t in allow)
        else:
            allowed.append(f"mcp__{key}__*")

    if specs:
        log.info("owui-mcp: injected %d server(s) for %s", len(specs), identity.user)
    return specs, allowed
