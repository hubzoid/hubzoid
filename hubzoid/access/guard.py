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

log = logging.getLogger("hubzoid.access")


def allowed_surfaces() -> frozenset[str]:
    """Surfaces that may reach restricted tools and personal connections."""
    raw = os.environ.get("HUBZOID_RESTRICTED_SURFACES", "").strip()
    if not raw:
        return DEFAULT_RESTRICTED_SURFACES
    return frozenset(s.strip().lower() for s in raw.split(",") if s.strip())


_allowed_surfaces = allowed_surfaces


def decide(hub_dir: Path, ident, permission: str,
           surfaces: "frozenset[str] | None" = None) -> tuple[bool, str]:
    """The ONE access decision, used by both the invocation guard and MCP tool
    discovery: the surface gate first, then the access store's `can()`.

    FAIL CLOSED: any error reading or evaluating the store denies."""
    if surfaces is None:
        surfaces = _allowed_surfaces()
    pre_allowed, reason = is_allowed(ident, permission, allowed_surfaces=surfaces, can=lambda: True)
    if not pre_allowed:
        return False, reason
    hub_dir = Path(hub_dir)
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
    surfaces = _allowed_surfaces() if surfaces is None else frozenset(surfaces)
    original_invoke = ft.on_invoke_tool
    hub_dir = Path(hub_dir)

    async def _guarded_invoke(ctx, input_str):
        ident = current_identity()
        allowed, reason = decide(hub_dir, ident, permission, surfaces)
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
        allowed, _ = decide(hub_dir, current_identity(), permission, surfaces)
        return allowed

    _is_enabled.hubzoid_permission = permission
    # Tools sharing a permission and surface rule share one decision per turn
    # (`visible_map`).
    _is_enabled.hubzoid_key = (str(hub_dir), permission, surfaces)
    return dataclasses.replace(ft, on_invoke_tool=_guarded_invoke, is_enabled=_is_enabled)


def visible(ft) -> bool:
    """Whether the current caller may see `ft`. False only for a tool this
    guard wraps whose permission the caller lacks; any other tool is visible.
    The same decision as the invoke wall, for runtimes that list tools
    themselves (Claude, Codex)."""
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
        check = getattr(ft, "is_enabled", True)
        if getattr(check, "hubzoid_permission", None) is None:
            out[name] = True
            continue
        key = getattr(check, "hubzoid_key", None) or ("fn", id(check))
        if key not in decided:
            decided[key] = bool(check())
        out[name] = decided[key]
    return out


def apply(hub_dir: Path, registry: dict) -> dict:
    """Merge guarded restricted tools into `registry`.

    Restricted tools win on name conflicts (a door is never silently shadowed by
    an unguarded built-in). Returns the registry unchanged when the hub has no
    `restricted/` folder.
    """
    restricted = load_restricted(Path(hub_dir))
    if not restricted:
        return registry
    out = dict(registry)
    for ft, permission in restricted:
        out[ft.name] = guard_tool(ft, permission, hub_dir)
    log.info(
        "access: %d restricted tool(s) under %d permission(s) loaded",
        len(restricted), len({p for _, p in restricted}),
    )
    return out
