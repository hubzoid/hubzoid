# Hubzoid Enterprise · MCP server. Production use requires a license with the
# "mcp" entitlement; free to run for development. See LICENSING.md.
"""Hosted MCP server — expose a hub's tools + knowledge over Streamable HTTP.

The inverse of `loaders/mcp.py` (which *consumes* MCP servers): this module
serves the hub's own FunctionTool registry to external MCP clients (Claude
Code, Cursor, claude.ai via OAuth later) so users can bring their own model
and use the hub purely for context and tools.

    claude mcp add --transport http myhub https://hub.example.com/mcp \
        --header "Authorization: Bearer sk-..."

Design points:

  * Opt-in per hub: ``MCP_SERVER=true`` in ``<hub>/.env`` (see settings.py).
    The bridge mounts the endpoint at ``/mcp``; the edge exposes it publicly
    (``/mcp`` single-hub, ``/b/<hub>/mcp`` in gateway mode).
  * Auth: Open WebUI per-user API keys, resolved read-only against OWUI's
    database (`access/owui_api_keys.py`). The caller's email then maps to
    their OWUI groups exactly like a chat request, and every tool call runs
    under `access.identity_scope` with surface ``mcp`` — restricted tools
    follow the same group rules, and every decision is audited. The bridge's
    own `BRIDGE_API_KEYS` are never accepted on this surface.
  * Tool set: the same registry the chat agent gets (built-ins + hub-local +
    guarded restricted/), minus chat-scoped tools (`write_artifact`,
    `read_upload`, ...) which have no chat directory to resolve outside a
    chat, and minus model-delegates (an MCP caller brings their own model;
    the hub does not spend inference on their behalf).
  * Instructions: the initialize response carries the hub's AGENTS.md body
    (or the ``mcp_instructions:`` frontmatter override) so the connecting
    agent learns what the hub is and how to use its tools — the MCP-native
    home for the system prompt it would otherwise never see.
  * Stateless Streamable HTTP: every request is self-contained, so the
    endpoint scales behind load balancers and survives bridge restarts.

"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from . import access
from . import memory as memlib
from . import settings as settingslib

log = logging.getLogger("hubzoid.mcp")

#: The identity surface MCP callers run under. Listed in
#: `access.policy.DEFAULT_RESTRICTED_SURFACES` because the caller carries a
#: per-person verified login (their own OWUI API key) — unlike Slack, where
#: one bot token speaks for everyone.
MCP_SURFACE = "mcp"

# Tools whose behaviour is bound to a live chat (ContextVar chat_id -> the
# chat's uploads/artifacts dirs). An MCP call has no chat, so exposing them
# would silently write to / read from the process fallback dir instead of
# doing what their descriptions promise. Excluded until they grow a
# non-chat scope.
CHAT_SCOPED_TOOLS = frozenset({
    "write_artifact",
    "list_artifacts",
    "read_upload",
    "read_upload_full",
})


# ---------------------------------------------------------------------------
# Registry: the hub's tools, built without any model backend.
# ---------------------------------------------------------------------------
def build_registry(
    hub_dir: Path, *, settings: "settingslib.Settings | None" = None
) -> tuple[dict, dict[str, str]]:
    """Return (registry, permissions) for the MCP surface.

    registry     {tool_name: FunctionTool} — built-ins + hub-local, gated by
                 access.apply (restricted tools arrive pre-guarded), minus
                 CHAT_SCOPED_TOOLS.
    permissions  {tool_name: permission} for restricted tools, used to hide
                 tools from `tools/list` for callers who may not use them.
                 (Hiding is UX; the guard's fail-closed invoke wall is the
                 actual enforcement.)

    Mirrors the front half of `factory.build_agent` but never touches a
    model: sub-agents are all promoted to skills (no delegates — a delegate
    runs on the hub's model budget, which an MCP caller does not get).
    """
    from . import factory
    from .loaders import knowledge as knowledge_loader
    from .loaders import tools_local as tools_local_loader
    from .tools import make_all as make_builtin_tools

    hub_dir = Path(hub_dir).resolve()
    if settings is None:
        settings = settingslib.load(hub_dir)

    session_id = memlib.make_session_id()
    ctx = factory.HubContext(
        hub_dir=hub_dir,
        output_dir=memlib.session_output_dir(hub_dir, session_id),
        session_id=session_id,
        settings=settings,
        skills=factory._load_skills_and_promoted_agents(hub_dir),
        knowledge=knowledge_loader.load_all(hub_dir),
        delegates=[],
    )

    # Per-user connections gate, same as the chat factories. Without this a
    # restricted tool calling connections.require() over MCP hits an unarmed
    # gate; inert unless the hub set CONNECTIONS.
    from . import connections as connlib
    connlib.attach(ctx)

    builtin = make_builtin_tools(ctx)
    local = tools_local_loader.load_all(hub_dir)
    registry = access.apply(hub_dir, {**builtin, **local})
    for name in CHAT_SCOPED_TOOLS:
        registry.pop(name, None)

    permissions = {
        ft.name: permission
        for ft, permission in access.load_restricted(hub_dir)
        if ft.name in registry
    }
    return registry, permissions


# ---------------------------------------------------------------------------
# Identity: OWUI api key -> email -> OWUI groups, per request.
# ---------------------------------------------------------------------------
def _mcp_identity(hub_dir: Path) -> "access.Identity":
    """The verified identity for the current MCP request.

    Reads the access token FastMCP's auth layer attached to this request and
    resolves the user's groups fresh from OWUI's database, so a group change
    in the admin panel applies to the caller's next call — same freshness as
    the chat path. Anonymous when no token is bound (should not happen once
    auth is configured, but fail-closed is the rule).
    """
    from fastmcp.server.dependencies import get_access_token

    try:
        token = get_access_token()
    except Exception:  # noqa: BLE001 — outside a request context
        token = None
    email = (token.claims or {}).get("email") if token is not None else None
    if not email:
        if token is not None:
            # Auth passed but the token carries no email — the caller runs as
            # anonymous (fail-closed for restricted tools, but the audit log
            # would misattribute their unrestricted calls). Should not happen
            # with our own verifier; make it visible if it ever does.
            log.warning("mcp: authenticated request resolved to anonymous identity")
        return access.ANONYMOUS
    # Union OWUI groups with the hub roster (keyed by the same email), so a
    # coordinator granted a permission in identity/access.* gets it over MCP
    # too. This governs RESTRICTED-TOOL permissions only. The MCP front door
    # (MCP_ACCESS_GROUP, in _build_verifier) stays OWUI-only on purpose: the
    # roster must not be able to open the gateway tenant boundary.
    groups = access.effective_groups(hub_dir, email=email, surface=MCP_SURFACE)
    return access.Identity.make(user=email, groups=groups, surface=MCP_SURFACE)


def _build_verifier(hub_dir: Path, *, access_group: str | None = None):
    """An auth provider that accepts OWUI per-user API keys as Bearer tokens.

    When `access_group` is set (MCP_ACCESS_GROUP), key holders must also be
    members of that OWUI group or the whole surface answers 401. This is the
    per-hub front door for gateway mode, where one shared user database backs
    every hub: without it, any logged-in user of any team could reach this
    hub's *unrestricted* tools and knowledge.
    """
    from fastmcp.server.auth import AccessToken, TokenVerifier

    from .access import owui_api_keys

    required = access.normalize(access_group or "")

    class _OwuiApiKeyVerifier(TokenVerifier):
        async def verify_token(self, token: str) -> "AccessToken | None":
            email = owui_api_keys.resolve_email(hub_dir, token)
            if not email:
                return None
            if required and required not in access.owui_groups.resolve_groups(
                hub_dir, email
            ):
                log.info(
                    "mcp: %s denied — not in MCP_ACCESS_GROUP %r", email, required
                )
                return None
            # claims["email"] is what `_mcp_identity` reads back per call.
            return AccessToken(
                token=token, client_id=email, scopes=[], claims={"email": email}
            )

    return _OwuiApiKeyVerifier()


# ---------------------------------------------------------------------------
# Tool adapter: openai-agents FunctionTool -> fastmcp Tool
# ---------------------------------------------------------------------------
# The access guard replies with this prefix instead of raising, so the model
# that reached a denied door gets a readable explanation (see guard.py). MCP
# clients branch on `isError`, so we translate the prefix to the error flag.
_DENIAL_PREFIX = "[access denied:"


_registry_tool_cls: Any = None


def _registry_tool_class():
    """The FastMCP Tool subclass wrapping a hub FunctionTool. Built lazily
    (importing this module must not require fastmcp unless MCP is enabled)
    and cached — one pydantic model build, reused for every tool."""
    global _registry_tool_cls
    if _registry_tool_cls is not None:
        return _registry_tool_cls

    import uuid

    from agents import RunConfig
    from agents.tool_context import ToolContext
    from fastmcp.tools import Tool
    from fastmcp.tools.tool import ToolResult
    from pydantic import PrivateAttr

    class _RegistryToolImpl(Tool):
        """Runs a FunctionTool under the MCP caller's identity.

        The run path mirrors `factory_claude._to_claude_tool`: a minimal
        valid ToolContext (hubzoid tools do not consult it, but the
        openai-agents dispatcher chain dereferences it), args passed as a
        JSON string. Identity is bound around the invoke so the access guard
        and the audit log see who is calling on which surface.
        """

        _ft: Any = PrivateAttr(default=None)
        _hub_dir: Any = PrivateAttr(default=None)

        async def run(self, arguments: dict[str, Any]) -> ToolResult:
            fn = self._ft
            args_json = json.dumps(arguments or {})
            tool_ctx = ToolContext(
                context=None,
                tool_name=fn.name,
                tool_call_id=f"hubzoid-mcp-{uuid.uuid4().hex[:12]}",
                tool_arguments=args_json,
                run_config=RunConfig(),
            )
            ident = _mcp_identity(self._hub_dir)
            with access.identity_scope(ident):
                try:
                    result = await fn.on_invoke_tool(tool_ctx, args_json)
                except Exception as exc:  # noqa: BLE001 — surface, don't crash the server
                    log.exception("mcp tool %s failed", fn.name)
                    return ToolResult(
                        content=f"[tool {fn.name} error: {type(exc).__name__}: {exc}]",
                        is_error=True,
                    )
            text = result if isinstance(result, str) else json.dumps(result, default=str)
            return ToolResult(
                content=text, is_error=text.startswith(_DENIAL_PREFIX)
            )

    _registry_tool_cls = _RegistryToolImpl
    return _registry_tool_cls


def _make_tool(ft, hub_dir: Path):
    """Wrap a FunctionTool as a FastMCP Tool, preserving its exact schema."""
    cls = _registry_tool_class()
    t = cls(
        name=ft.name,
        description=ft.description or f"Tool: {ft.name}",
        parameters=ft.params_json_schema or {"type": "object", "properties": {}},
    )
    t._ft = ft
    t._hub_dir = Path(hub_dir)
    return t


def _build_list_filter(hub_dir: Path, permissions: dict[str, str]):
    """Middleware hiding restricted tools from callers who may not use them.

    Purely cosmetic scoping (no leaked names/schemas, no wasted client turns)
    — the enforcement wall is the guard wrapped around each restricted
    tool's invoke, which fails closed and audits regardless of listing.
    """
    from fastmcp.server.middleware import Middleware

    from .access.guard import _allowed_surfaces

    class _AccessListFilter(Middleware):
        async def on_list_tools(self, context, call_next):
            tools = await call_next(context)
            if not permissions:
                return tools
            ident = _mcp_identity(hub_dir)
            surfaces = _allowed_surfaces()
            return [
                t for t in tools
                if access.is_allowed(
                    ident, permissions.get(t.name, ""), allowed_surfaces=surfaces
                )[0]
            ]

    return _AccessListFilter()


# ---------------------------------------------------------------------------
# Instructions + assembly
# ---------------------------------------------------------------------------
def _instructions(hub_dir: Path) -> str | None:
    """What the connecting agent is told about the hub at initialize time.

    ``mcp_instructions:`` in AGENTS.md frontmatter wins (for hubs whose
    system prompt contains internal-only guidance, or is just too long to
    spend on every client's context); otherwise the AGENTS.md body — the
    same instructions the hub's own runtime lives by.
    """
    from . import frontmatter as fm

    try:
        data, body = fm.read(Path(hub_dir) / "AGENTS.md")
    except Exception:  # noqa: BLE001 — a hub without AGENTS.md still serves tools
        return None
    custom = data.get("mcp_instructions")
    if isinstance(custom, str) and custom.strip():
        return custom.strip()
    return body.strip() or None


def _agent_name(hub_dir: Path) -> str:
    from . import frontmatter as fm

    try:
        data, _ = fm.read(Path(hub_dir) / "AGENTS.md")
        return str(data.get("name") or Path(hub_dir).name)
    except Exception:  # noqa: BLE001
        return Path(hub_dir).name


def build_mcp_app(
    hub_dir: Path,
    *,
    path: str = "/mcp",
    settings: "settingslib.Settings | None" = None,
):
    """Build the ASGI app serving this hub over MCP Streamable HTTP.

    Returns a Starlette app with a single route at `path` and a `.lifespan`
    the host app must run (the bridge composes it into its own lifespan —
    without it the session manager is never started and every request 500s).
    """
    from fastmcp import FastMCP

    hub_dir = Path(hub_dir).resolve()
    if settings is None:
        settings = settingslib.load(hub_dir)
    registry, permissions = build_registry(hub_dir, settings=settings)

    mcp = FastMCP(
        name=_agent_name(hub_dir),
        instructions=_instructions(hub_dir),
        auth=_build_verifier(hub_dir, access_group=settings.mcp_access_group),
        middleware=[_build_list_filter(hub_dir, permissions)],
    )
    for ft in registry.values():
        mcp.add_tool(_make_tool(ft, hub_dir))

    log.info("mcp: serving %d tool(s) at %s (stateless)", len(registry), path)
    return mcp.http_app(path=path, stateless_http=True)
