# Hubzoid MCP server. Apache-2.0 licensed like the rest of the repository.
"""Hosted MCP server — expose a hub's tools + knowledge over Streamable HTTP.

The inverse of `loaders/mcp.py` (which *consumes* MCP servers): this module
serves the hub's own FunctionTool registry to external MCP clients (Claude
Code, Cursor, claude.ai via OAuth) so users can bring their own model
and use the hub purely for context and tools.

    claude mcp add --transport http myhub https://hub.example.com/mcp

Design points:

  * Opt-in per hub: ``MCP_SERVER=true`` in ``<hub>/.env`` (see settings.py).
    The bridge mounts the endpoint at ``/mcp``; the edge exposes it publicly
    (``/mcp`` single-hub, ``/b/<hub>/mcp`` in gateway mode).
  * Auth: OAuth authorization code flow with PKCE. Open WebUI authenticates
    the user; Hubzoid collects consent and issues scoped OAuth credentials.
    Static API keys and bridge secrets are rejected. Every request rechecks
    the current account and hub permissions; tools run under identity_scope
    with surface ``mcp`` and the same access rules as chat.
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
#: per-person verified login (their own OAuth grant) — unlike Slack, where
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
    from . import hitl
    registry = hitl.equip(hub_dir, registry, questions=False)
    from .apps import cards
    registry = cards.wrap(hub_dir, registry)
    for name in CHAT_SCOPED_TOOLS:
        registry.pop(name, None)
    # Read-only admin tools, for agent administrators only (MCP surface only).
    from .admin import mcp_tools as admin_tools

    for ft in admin_tools.make(hub_dir):
        registry.setdefault(ft.name, ft)

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

    Reads the access token FastMCP's auth layer attached to this request (the
    person who signed in over OAuth). Their grants are read fresh on every
    call, so a change in the Console applies to their next call. Anonymous
    when no token is bound (should not happen once auth is configured, but
    fail-closed is the rule).
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
    groups = access.effective_groups(hub_dir, email=email, surface=MCP_SURFACE)
    return access.Identity.make(user=email, groups=groups, surface=MCP_SURFACE,
                                account_id=getattr(token, "subject", None) or None)


def _mcp_grant_id() -> str:
    from fastmcp.server.dependencies import get_access_token

    try:
        token = get_access_token()
    except Exception:  # noqa: BLE001
        return ""
    return str((token.claims or {}).get("grant_id") or "")[:16] if token is not None else ""





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
            if ident.user:
                from .access import run_identity

                try:
                    record = run_identity.admit_or_refuse(
                        self._hub_dir, run_kind="mcp", run_id=tool_ctx.tool_call_id,
                        acts_as_id=ident.account_id, acts_as_email=ident.user, started_from="mcp",
                        person_present=False, credential_kind="mcp_grant",
                        credential_id=_mcp_grant_id())
                except run_identity.NotAdmitted as exc:
                    return ToolResult(content=f"[{exc}]", is_error=True)
                ident = run_identity.identity(record, MCP_SURFACE, ident.groups)
            from . import _request_ctx

            with access.identity_scope(ident), _request_ctx.card_scope():
                try:
                    result = await fn.on_invoke_tool(tool_ctx, args_json)
                except Exception as exc:  # noqa: BLE001 — surface, don't crash the server
                    log.exception("mcp tool %s failed", fn.name)
                    return ToolResult(
                        content=f"[tool {fn.name} error: {type(exc).__name__}: {exc}]",
                        is_error=True,
                    )
                text = result if isinstance(result, str) else json.dumps(result, default=str)
                card = _request_ctx.take_card(text, tool=fn.name)
            if card is not None:
                # A card: the text for the model, the checked result for the
                # host's card, and the instance its frame refreshes by.
                from .apps import cards

                result_ = cards.call_tool_result(card)
                return ToolResult(content=text, structured_content=result_["structuredContent"],
                                  meta=result_["_meta"])
            return ToolResult(
                content=text, is_error=text.startswith(_DENIAL_PREFIX)
            )

    _registry_tool_cls = _RegistryToolImpl
    return _registry_tool_cls


_dispatch_registry: dict[str, tuple[float, dict]] = {}


async def call_registry_tool(hub_dir: Path, name: str, arguments: dict, identity,
                             classes: tuple[str, ...] | None) -> dict:
    """Run one hub tool outside a chat (a card frame, ``hubzoid apps invoke``),
    as ``identity``, through the same guards as any call. With ``classes``,
    only tools of those classes run (a card frame only reads). Returns
    ``{"text", "card"}`` (the card the result showed, if any) or
    ``{"refused", "message"}``."""
    import time
    import uuid

    from agents import RunConfig
    from agents.tool_context import ToolContext

    from . import tool_classes

    key = str(Path(hub_dir).resolve())
    hit = _dispatch_registry.get(key)
    if hit is None or time.monotonic() - hit[0] > 30:
        registry, _ = build_registry(Path(hub_dir))
        hit = (time.monotonic(), registry)
        _dispatch_registry[key] = hit
    ft = hit[1].get(name)
    if ft is None:
        return {"refused": "unknown_tool", "message": "That tool is not available here."}
    if classes is not None and tool_classes.classify(ft) not in classes:
        return {"refused": "write_class", "message": "A card can only read. Do this in Hub, where it is "
                "checked and recorded."}
    args_json = json.dumps(arguments or {})
    tool_ctx = ToolContext(context=None, tool_name=name, tool_call_id=f"hubzoid-card-{uuid.uuid4().hex[:12]}",
                           tool_arguments=args_json, run_config=RunConfig())
    from . import _request_ctx

    from .apps import cards
    from contextlib import nullcontext

    # A card frame's call returns the tool's own result, for the card it shows.
    raw = cards.raw_results() if classes is not None else nullcontext()
    with access.identity_scope(identity), _request_ctx.card_scope(), raw:
        result = await ft.on_invoke_tool(tool_ctx, args_json)
        text = result if isinstance(result, str) else json.dumps(result, default=str)
        return {"text": text, "card": _request_ctx.take_card(text, tool=name)}


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


def _build_list_filter(hub_dir: Path, permissions: dict[str, str],
                       registry: dict | None = None):
    """Middleware hiding gated tools from callers who may not use them.

    Two kinds: restricted tools (`permissions`, by name) and built-in tools
    whose `is_enabled` carries a `hubzoid_permission` marker (the capability
    tools and the management tools), asked through `guard.visible` under the
    caller's identity, exactly as the Claude and Codex runtimes ask per turn.

    Purely cosmetic scoping (no leaked names/schemas, no wasted client turns)
    — the enforcement wall is the guard wrapped around each tool's invoke,
    which fails closed and audits regardless of listing.
    """
    from fastmcp.server.middleware import Middleware

    from .access.guard import _allowed_surfaces, decide, visible_map

    gated = {
        name: ft for name, ft in (registry or {}).items()
        if name not in permissions
        and getattr(getattr(ft, "is_enabled", None), "hubzoid_permission", None) is not None
    }
    # Tools gated by a platform capability (the read-only admin tools).
    by_capability = {name for name, ft in (registry or {}).items()
                     if getattr(ft, "hubzoid_capability", None)}

    class _AccessListFilter(Middleware):
        async def on_list_tools(self, context, call_next):
            tools = await call_next(context)
            if not permissions and not gated and not by_capability:
                return tools
            ident = _mcp_identity(hub_dir)
            if by_capability:
                from .admin import mcp_tools as admin_tools

                if not admin_tools.allowed(hub_dir, ident.user):
                    tools = [t for t in tools if t.name not in by_capability]
            surfaces = _allowed_surfaces()
            # Use the SAME fail-closed decision as invocation, so a user sees a
            # tool exactly when invoking it would be allowed.
            shown = []
            with access.identity_scope(ident):
                try:
                    vis = visible_map({t.name: gated[t.name] for t in tools if t.name in gated})
                except Exception:  # noqa: BLE001 — hide on doubt; invoke still decides
                    log.exception("mcp: visibility check failed")
                    vis = {}
                for t in tools:
                    if t.name in by_capability:
                        ok = True  # already filtered above; the tool checks again on invoke
                    elif t.name in gated:
                        ok = vis.get(t.name, False)
                    else:
                        ok = decide(hub_dir, ident, permissions.get(t.name, ""), surfaces)[0]
                    if ok:
                        shown.append(t)
            return shown

    return _AccessListFilter()


# ---------------------------------------------------------------------------
# Cards (MCP Apps): tool metadata, ui:// resources, the status tool
# ---------------------------------------------------------------------------
def _visible(hub_dir: Path, ident, name: str, registry: dict, permissions: dict[str, str]) -> bool:
    """Whether ``ident`` may use the tool ``name``: the same decision as
    tools/list and invoke (restricted permission, or the tool's own gate)."""
    from .access.guard import _allowed_surfaces, decide, visible_map

    ft = registry.get(name)
    if ft is None or ident.is_anonymous:
        return False
    if getattr(getattr(ft, "is_enabled", None), "hubzoid_permission", None) is not None \
            and name not in permissions:
        with access.identity_scope(ident):
            return bool(visible_map({name: ft}).get(name, False))
    return decide(hub_dir, ident, permissions.get(name, ""), _allowed_surfaces())[0]


def _add_cards(mcp, hub_dir: Path, registry: dict, permissions: dict[str, str]) -> None:
    """Publish the active cards: ``_meta.ui`` and an output schema on each bound
    tool (read per listing, so an applied change shows without a restart), a
    ``ui://`` resource template read only by callers who may use the tool, and
    the app-visible ``hub_app_status`` refresh."""
    from fastmcp.resources import ResourceContent, ResourceResult
    from fastmcp.server.middleware import Middleware
    from fastmcp.tools.base import ToolResult as _ToolResult

    from .apps import cards
    from .apps import config as cfg

    class _CardMeta(Middleware):
        async def on_list_tools(self, context, call_next):
            tools = await call_next(context)
            _row, res = cards.active(hub_dir)
            if res is None or not res.enabled:
                return tools
            hub = cards.hub_key(hub_dir)
            by_tool = {c.tool: c for c in res.cards.values()}
            out = []
            for t in tools:
                card = by_tool.get(t.name)
                if card is not None:
                    meta = {**(t.meta or {}), "ui": {"resourceUri": card.uri(hub), "visibility": card.visibility}}
                    update = {"meta": meta}
                    if card.schema is not None:
                        update["output_schema"] = card.schema
                    t = t.model_copy(update=update)
                out.append(t)
            return out

    mcp.add_middleware(_CardMeta())

    @mcp.resource("ui://{hub}/{card_id}/{sha256}/index.html", mime_type=cfg.MIME,
                  name="hubzoid-card", description="A Hubzoid card (MCP App).")
    def card_resource(hub: str, card_id: str, sha256: str):
        _row, res = cards.active(hub_dir)
        card = res.cards.get(card_id) if res and res.enabled else None
        if hub != cards.hub_key(hub_dir) or card is None:
            raise ValueError("No such card here.")
        if not _visible(hub_dir, _mcp_identity(hub_dir), card.tool, registry, permissions):
            raise ValueError("No such card here.")
        # An earlier version an old conversation shows is still served; its calls are refused.
        html = card.html if card.sha256 == sha256 else cards.retained_html(hub_dir, card_id, sha256)
        if html is None:
            raise ValueError("No such card here.")
        return ResourceResult(contents=[ResourceContent(html, mime_type=cfg.MIME,
                                                        meta={"ui": cards.resource_meta()})])

    @mcp.tool(name=cards.APP_STATUS_TOOL, meta={"ui": {"visibility": ["app"]}},
              description="Refresh a Hubzoid progress or approval card. Called by the card itself.")
    def hub_app_status(instance_id: str):
        ident = _mcp_identity(hub_dir)
        from .work_items.errors import WorkItemError

        try:
            inst = cards.instance(hub_dir, instance_id, ident.user or "")
            cards.limit(hub_dir, ident.user or "", inst)
        except WorkItemError as exc:
            return _ToolResult(content=exc.message, is_error=True)
        out = cards._status_result(hub_dir, inst, ident.user or "")
        return _ToolResult(content=out["content"][0]["text"], structured_content=out.get("structuredContent"),
                           is_error=bool(out.get("isError")))


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
    hub_dir = Path(hub_dir).resolve()
    if settings is None:
        settings = settingslib.load(hub_dir)
    from .mcp_oauth import HubOAuth

    mcp, count = build_server(hub_dir, settings=settings, auth=HubOAuth(hub_dir, settings.mcp_public_url))
    log.info("mcp: serving %d tool(s) at %s (stateless)", count, path)
    return mcp.http_app(path=path, stateless_http=True)


def build_server(hub_dir: Path, *, settings: "settingslib.Settings | None" = None, auth=None):
    """The FastMCP server for this hub (tools, and cards when configured), and
    its tool count. ``build_mcp_app`` serves it over HTTP with OAuth."""
    from fastmcp import FastMCP

    hub_dir = Path(hub_dir).resolve()
    registry, permissions = build_registry(hub_dir, settings=settings)
    mcp = FastMCP(
        name=_agent_name(hub_dir),
        instructions=_instructions(hub_dir),
        auth=auth,
        middleware=[_build_list_filter(hub_dir, permissions, registry)],
    )
    for ft in registry.values():
        mcp.add_tool(_make_tool(ft, hub_dir))
    from .apps import cards as cards_lib

    if cards_lib.has_config(hub_dir):
        _add_cards(mcp, hub_dir, registry, permissions)
    return mcp, len(registry)
