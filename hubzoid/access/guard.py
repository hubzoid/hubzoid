# Hubzoid access management. Apache-2.0, like the rest of the repo (see LICENSING.md).
"""Wrap a restricted FunctionTool so the gate runs in code before it executes.

Two layers, exactly as the design says, and both backends get them because both
factories build from the same FunctionTool registry:

  * is_enabled  -> hides the tool from a caller who lacks the permission. The
                   OpenAI Agents SDK evaluates this per run, so the agent is
                   never even shown a door it cannot open (no wasted turn, no
                   leak of the tool's name and schema to the unauthorized).
  * on_invoke   -> re-checks at call time and fails closed, writing the decision
                   to the audit log (a call whose row cannot be written is
                   refused). This is the wall: it holds even if the tool
                   is reached another way (a prompt injection naming it, the
                   Claude path which does not consult is_enabled, or a test).

The Claude and Codex runtimes do not evaluate `is_enabled` themselves, so they
ask `visible()` per turn and leave hidden tools out of what the model is shown.

`apply` is the one entry point the factories call. With no `restricted/` folder
it returns the registry unchanged, so nothing about an existing hub moves.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
from pathlib import Path

from agents.tool import FunctionTool

from . import audit
from .identity import current_identity
from .loader import load_restricted
from .policy import DEFAULT_RESTRICTED_SURFACES, is_allowed

from ..tool_classes import copy_tool

log = logging.getLogger("hubzoid.access")


def allowed_surfaces() -> frozenset[str]:
    """Surfaces that may reach restricted tools and personal connections."""
    raw = os.environ.get("HUBZOID_RESTRICTED_SURFACES", "").strip()
    if not raw:
        return DEFAULT_RESTRICTED_SURFACES
    return frozenset(s.strip().lower() for s in raw.split(",") if s.strip())


_allowed_surfaces = allowed_surfaces


def decide(hub_dir: Path, ident, permission: str,
           surfaces: "frozenset[str] | None" = None, *, tool: str | None = None) -> tuple[bool, str]:
    """The ONE access decision, used by both the invocation guard and MCP tool
    discovery: the surface gate first, then the access store's `can()`.

    A person an embedding app vouched for is judged on the app's limit instead
    (``evaluator.app_access``): ``tool`` or ``permission`` must be within it.

    FAIL CLOSED: any error reading or evaluating the store denies."""
    if surfaces is None:
        surfaces = _allowed_surfaces()
    pre_allowed, reason = is_allowed(ident, permission, allowed_surfaces=surfaces, can=lambda: True)
    if not pre_allowed:
        return False, reason
    hub_dir = Path(hub_dir)
    app = getattr(ident, "app", None)
    if app is not None:
        from .evaluator import app_access

        decision = app_access(hub_dir, app, permission, tool=tool)
        return (True, "app") if decision.allowed else (False, decision.reason)
    hub_name = hub_dir.name
    subject = getattr(ident, "user", None) or ""
    from . import store_for

    try:
        gs = store_for(hub_dir)
        if gs.is_suspended(subject):
            return False, "blocked"
        allowed = gs.can(subject, hub_name, permission)
    except Exception:  # noqa: BLE001 — can't decide -> deny, don't guess
        log.exception("access: store unavailable for %s; denying", hub_name)
        return (False, "store-error")
    return is_allowed(ident, permission, allowed_surfaces=surfaces, can=lambda: allowed)


def _named(hub_dir: Path, permission: str) -> str:
    """The capability as the Console names it, with the id: "Call Jev" (jev)."""
    try:
        from ..capabilities import catalog

        for entry in catalog(hub_dir):
            if entry["permission"] == permission and entry.get("label") not in (None, "", permission):
                return f'"{entry["label"]}" ({permission})'
    except Exception:  # noqa: BLE001 — wording only
        log.debug("access: no label for %s", permission, exc_info=True)
    return f"'{permission}'"


def guard_tool(ft: FunctionTool, permission: str, hub_dir: Path, *,
               surfaces: "frozenset[str] | None" = None) -> FunctionTool:
    """Return a guarded copy of `ft` that enforces `permission`.

    The original is left untouched (`dataclasses.replace` copies it). The
    decision is read from the per-request identity at call time, so one guarded
    instance built at boot serves every user correctly.

    `surfaces` replaces the restricted-tool surface policy for this tool (a
    tool family with its own rule, e.g. the management tools'
    `service.TOOL_SURFACES`). Default: `allowed_surfaces()`.
    """
    from .. import tool_fakes as fakes

    surfaces = _allowed_surfaces() if surfaces is None else frozenset(surfaces)
    # An eval's fake answers only after this check allows the call.
    ft = fakes.wrap(ft)
    original_invoke = ft.on_invoke_tool
    hub_dir = Path(hub_dir)

    async def _guarded_invoke(ctx, input_str):
        ident = current_identity()
        allowed, reason = decide(hub_dir, ident, permission, surfaces, tool=ft.name)
        recorded = await asyncio.to_thread(
            audit.record, hub_dir, user=ident.user, surface=ident.surface, tool=ft.name,
            decision=("allow" if allowed else "deny"), reason=reason,
        )
        if allowed and not recorded:
            # Every restricted call that runs has an audit row: no row, no call.
            return (
                f"[access denied: '{ft.name}' could not be recorded in the access "
                "log, so it was not run. Try again shortly.]"
            )
        if not allowed:
            # Say "logged" only when the row exists. Either way the call is refused.
            return (
                f"[access denied: '{ft.name}' requires the {_named(hub_dir, permission)} "
                "permission, which the current user does not have. "
                + ("This attempt was logged.]" if recorded else
                   "This attempt could not be logged.]")
            )
        return await original_invoke(ctx, input_str)

    def _is_enabled(*_args, **_kwargs) -> bool:
        # The SDK calls this with (run_context, agent); we only need the
        # request-scoped identity, so accept anything and ignore it.
        allowed, _ = decide(hub_dir, current_identity(), permission, surfaces, tool=ft.name)
        return allowed

    _is_enabled.hubzoid_permission = permission
    # Tools sharing a permission and surface rule share one decision per turn
    # (`visible_map`).
    _is_enabled.hubzoid_key = (str(hub_dir), permission, surfaces)
    return fakes.covered(copy_tool(ft, on_invoke_tool=_guarded_invoke, is_enabled=_is_enabled))


def _app_hides(name: str, ft=None) -> bool:
    """A person an embedding app vouched for sees only the tools within their
    access (the token's tools; the app's current limit is checked again at
    call time)."""
    app = getattr(current_identity(), "app", None)
    if app is None:
        return False
    from ..embed.caller import tool_allowed
    from ..embed.config import Access

    # An MCP tool is judged with the server or connector it comes from
    # (``run_checked`` and ``runtime.open_personal_mcp`` record it on the tool).
    return not tool_allowed(Access(agents=app.agents, tools=app.tools), name,
                            server=getattr(ft, "hubzoid_server", None),
                            connector=getattr(ft, "hubzoid_connector", None))


def visible(ft) -> bool:
    """Whether the current caller may see `ft`. False only for a tool this
    guard wraps whose permission the caller lacks; any other tool is visible.
    The same decision as the invoke wall, for runtimes that list tools
    themselves (Claude, Codex)."""
    if _app_hides(getattr(ft, "name", ""), ft):
        return False
    check = getattr(ft, "is_enabled", True)
    if getattr(check, "hubzoid_permission", None) is None:
        return True
    return bool(check())


def visible_map(tools: dict) -> dict[str, bool]:
    """`visible` for a whole registry at once ({name: shown}), for runtimes
    that filter the tool list every turn. Each distinct check runs once: tools
    guarded by the same permission and surface rule (`hubzoid_key`), or
    sharing one `is_enabled` (the management tools), get one decision. Called
    per turn, so a grant or revocation shows on the next turn."""
    decided: dict = {}
    out: dict[str, bool] = {}
    for name, ft in tools.items():
        if _app_hides(name, ft):
            out[name] = False
            continue
        check = getattr(ft, "is_enabled", True)
        if getattr(check, "hubzoid_permission", None) is None:
            out[name] = True
            continue
        key = getattr(check, "hubzoid_key", None) or ("fn", id(check))
        if key not in decided:
            decided[key] = bool(check())
        out[name] = decided[key]
    return out


REVOKED_MESSAGE = ("[access ended: this account's access to this agent was removed or its "
                   "sign-in was revoked, so '{name}' was not run.]")


# The run's account or credential ended (removed, revoked, signed out, out of
# the key's agents): the run stops. Any other refusal is an ordinary "no".
ENDED_REASONS = frozenset({"account_inactive", "credentials_revoked", "credential_ended", "key_scope"})
DENIED_MESSAGE = ("[access denied: '{name}' needs access to this agent, which the current "
                  "user does not have.]")
UNCHECKED_MESSAGE = ("[access denied: access could not be checked, so '{name}' was not run. "
                     "Try again shortly.]")
APP_TOOL_MESSAGE = ("[access denied: '{name}' is not one of the tools this app allows, so it was "
                    "not run.]")


def refusal(decision, name: str, *, guarded: bool = False) -> str | None:
    """The words a refused call answers with, or None to let it run."""
    if decision.allowed:
        return None
    log.info("access: %s refused at call time (%s)", name, decision.reason)
    if decision.reason in ENDED_REASONS:
        return REVOKED_MESSAGE.format(name=name)
    if decision.reason == "store_error":
        return UNCHECKED_MESSAGE.format(name=name)
    if decision.reason == "app_tool":
        return APP_TOOL_MESSAGE.format(name=name)
    return None if guarded else DENIED_MESSAGE.format(name=name)


STOPPED = "This reply was stopped, so the tool did not run."


async def _stopped() -> bool:
    """The chat run this call belongs to was stopped or lost its branch lease
    (``SessionSlot.guard``). Checked here, in the wrapper every runtime's hub
    and MCP tools pass through, so Stop holds on Claude, Codex and the Agents
    SDK alike. Fails closed: no step without a known owner."""
    from .. import _request_ctx

    guard = getattr(_request_ctx.native_session(), "guard", None)
    if guard is None:
        return False
    try:
        return not await asyncio.to_thread(guard)
    except Exception:  # noqa: BLE001
        return True


# A Settings connector's server key (``connectors.per_user.server_key``).
_CONNECTOR_KEY = "my_"


def server_source(hub_dir: Path, name: str) -> tuple[str | None, str | None]:
    """(hub MCP server, Settings connector) a server name stands for. A hub
    server keeps its own name even when it starts with ``my_``: only a name the
    hub doesn't use itself is a connector's (``connectors.per_user``)."""
    from ..personal_mcp import _hub_server_keys

    if not name:
        return None, None
    if name.startswith(_CONNECTOR_KEY) and name not in _hub_server_keys(hub_dir):
        return None, name[len(_CONNECTOR_KEY):]
    return name, None


def checked_server(server, hub_dir: Path):
    """``server`` (an Agents SDK MCP server) with the same per-call check as
    ``run_checked``. Every runtime's MCP tools end in ``server.call_tool``,
    including servers connected after the registry was built (a hub's MCP
    servers on Codex and the Agents SDK, and personal connectors)."""
    original = server.call_tool
    if getattr(original, "hubzoid_run_checked", False):
        return server
    hub_dir = Path(hub_dir)

    async def call_tool(tool_name, arguments, *args, **kwargs):
        from mcp.types import CallToolResult, TextContent

        from .evaluator import check_run

        if await _stopped():
            return CallToolResult(content=[TextContent(type="text", text=STOPPED)], isError=True)
        name = str(getattr(server, "name", "") or "")
        hub_server, connector = await asyncio.to_thread(server_source, hub_dir, name)
        decision = await asyncio.to_thread(check_run, hub_dir, current_identity(), tool=str(tool_name),
                                           server=hub_server, connector=connector)
        refused = refusal(decision, tool_name)
        if refused is not None:
            return CallToolResult(content=[TextContent(type="text", text=refused)], isError=True)
        result = await original(tool_name, arguments, *args, **kwargs)
        from ..embed import scope as app_scope

        if app_scope.live_token():
            for part in getattr(result, "content", None) or []:
                if getattr(part, "type", None) == "text" and isinstance(getattr(part, "text", None), str):
                    part.text = app_scope.scrub(part.text)
            if getattr(result, "structuredContent", None) is not None:
                result.structuredContent = app_scope.scrub(result.structuredContent)
        return result

    call_tool.hubzoid_run_checked = True
    server.call_tool = call_tool
    return server


def run_checked(ft: FunctionTool, hub_dir: Path, *, server: str | None = None) -> FunctionTool:
    """A copy of ``ft`` that first checks the run's account may still use the
    hub (``evaluator.check_run``: active, same credential counter, not
    blocked, ``use_hub``). Allowed means it runs at once; nothing asks a person.

    ``server``: the MCP server the tool comes from (a hub server, or a Settings
    connector's ``my_<id>``), so an embedding app's limit can name it."""
    invoke = getattr(ft, "on_invoke_tool", None)
    if invoke is None or not dataclasses.is_dataclass(ft) or getattr(invoke, "hubzoid_run_checked", False):
        return ft
    original_invoke = ft.on_invoke_tool
    hub_dir = Path(hub_dir)
    guarded = getattr(getattr(ft, "is_enabled", None), "hubzoid_permission", None) is not None
    hub_server, connector = server_source(hub_dir, server) if server else (None, None)

    async def _checked_invoke(ctx, input_str):
        from .evaluator import check_run

        if await _stopped():
            return STOPPED
        decision = await asyncio.to_thread(check_run, hub_dir, current_identity(), tool=ft.name,
                                           server=hub_server, connector=connector)
        refused = refusal(decision, ft.name, guarded=guarded)
        if refused is not None:
            return refused
        # Allowed, or a restricted tool that refuses (and logs) in its own words.
        from ..embed import scope as app_scope

        with app_scope.tool_scope(ft.name):
            return app_scope.scrub(await original_invoke(ctx, input_str))

    _checked_invoke.hubzoid_run_checked = True
    changes = {"on_invoke_tool": _checked_invoke}
    if getattr(ft, "is_enabled", True) is True:
        # The Agents SDK lists tools itself: a person an app vouched for is
        # shown only the tools within their access.
        def _is_enabled(*_args, **_kwargs) -> bool:
            return not _app_hides(ft.name, out)

        changes["is_enabled"] = _is_enabled
    out = copy_tool(ft, **changes)
    if hub_server:
        out.hubzoid_server = hub_server
    if connector:
        out.hubzoid_connector = connector
    return out


def apply(hub_dir: Path, registry: dict) -> dict:
    """Merge guarded restricted tools into `registry`, and make every tool
    recheck the run's account before it runs (``run_checked``).

    Restricted tools win on name conflicts (a door is never silently shadowed by
    an unguarded built-in). Every runtime's factory calls this, so the check is
    the same on Claude, Codex and the Agents SDK.
    """
    restricted = load_restricted(Path(hub_dir))
    out = dict(registry)
    for ft, permission in restricted:
        out[ft.name] = guard_tool(ft, permission, hub_dir)
    if restricted:
        log.info(
            "access: %d restricted tool(s) under %d permission(s) loaded",
            len(restricted), len({p for _, p in restricted}),
        )
    return {name: run_checked(ft, hub_dir) for name, ft in out.items()}
