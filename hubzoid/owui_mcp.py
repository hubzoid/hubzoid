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
kill-switch ``OWUI_NATIVE_MCP=0`` is set. Nothing here writes; every read is
read-only and fail-closed via the access layer. See the design doc
``docs/per-user-tool-connections.html``.

Not yet handled here: token refresh (task tracked separately). A connected
token is used as stored; when it expires the caller reconnects in OWUI until
the refresh path lands.
"""
from __future__ import annotations

import logging
import os
import re
import time

from .access import owui_oauth_tokens as tokens
from .access import owui_tool_servers as servers

log = logging.getLogger("hubzoid.owui_mcp")

_NAME_RE = re.compile(r"[^a-z0-9]+")

# Skip a token this close to (or past) expiry. Until the refresh path lands
# (tracked separately) an expired token is dropped rather than injected to
# 401 — the caller reconnects in OWUI. A small skew avoids racing the clock.
_EXPIRY_SKEW_SECONDS = 60


def _expired(token: dict) -> bool:
    """True when OWUI's stored token is at or past its expiry (minus skew)."""
    exp = token.get("expires_at")
    if not exp:
        return False
    try:
        return time.time() >= float(exp) - _EXPIRY_SKEW_SECONDS
    except (TypeError, ValueError):
        return False


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
        token = tokens.read_token(hub_dir, user_id, server_id)
        access_token = (token or {}).get("access_token")
        if not access_token:
            continue
        if _expired(token):
            log.info("owui-mcp: token for %r expired; caller must reconnect", server_id)
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
