"""The personal MCP servers of the current turn, for all three runtimes.

Kept under its 1.1 name so the runtime adapters do not move. Since 1.2 every
UI mode uses the connectors added in the Console (``hubzoid.connectors``): Open
WebUI's own MCP servers are no longer read. The rules (signed-in person,
allowed surface, not blocked, offered in this agent, the ``connector_<id>``
grant, a usable token) live in ``hubzoid.connectors.per_user``.

It returns plain data (:class:`PerUserServer`), and each runtime adapter builds
its own client from it inside the turn:

  * Claude (``factory_claude.ClaudeRuntime``) merges :func:`per_user_specs`,
    the Claude-SDK ``http`` specs, into its per-turn options.
  * OpenAI Agents (``runtime.OpenAIAgentsRuntime``) connects a Streamable HTTP
    client per server and runs a per-turn clone of the agent.
  * Codex (``factory_codex.CodexRuntime``) connects the same clients and adds
    their tools to a per-turn copy of its registry.

So two people on the same hub each reach the same MCP server as themselves and
see only their own data, whichever backend the hub runs on.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger("hubzoid.owui_mcp")

_APP_RE = re.compile(r"[^a-z0-9_]+")

CONNECTOR_PREFIX = "connector_"

# The Claude runtime's own in-process server key (factory_claude._MCP_NAMESPACE).
_HUB_SERVER_KEY = "hubzoid"


def app_key(name: str) -> str:
    """The app a connector is known by: lowercase ``[a-z0-9_]`` (its ID)."""
    return _APP_RE.sub("_", (name or "").strip().lower()).strip("_")


def capability(app: str) -> str:
    """The capability that gates a connector: ``connector_<app>``."""
    return f"{CONNECTOR_PREFIX}{app_key(app)}"


@dataclass(frozen=True)
class PerUserServer:
    """One MCP server this caller may reach this turn, as themselves.

    Runtime-neutral: ``headers`` carries the caller's credential, and
    ``allowed_tools`` is the admin's tool allow-list (bare MCP tool names) or
    None for no filter. Never log or return ``headers``.
    """

    key: str
    url: str
    headers: dict = field(repr=False)
    allowed_tools: tuple[str, ...] | None
    server_id: str
    app: str


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
    """A per-server check for this caller: each server needs ``connector_<app>``
    in this hub, through ``guard.decide`` (fails closed)."""
    from .access.guard import decide

    return lambda app: decide(hub_dir, identity, capability(app))[0]


def per_user_servers(hub_dir, identity, *, reserved: set[str] | None = None) -> list[PerUserServer]:
    """The MCP servers this caller may use this turn (``reserved`` are server
    keys that must not be replaced, default the hub's own). Empty on every
    refusal path, never raises for missing data."""
    from .connectors.per_user import per_user_servers as connector_servers

    return connector_servers(hub_dir, identity, reserved=reserved)


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
