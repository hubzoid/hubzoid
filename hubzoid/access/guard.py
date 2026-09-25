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


def _allowed_surfaces() -> frozenset[str]:
    raw = os.environ.get("HUBZOID_RESTRICTED_SURFACES", "").strip()
    if not raw:
        return DEFAULT_RESTRICTED_SURFACES
    return frozenset(s.strip().lower() for s in raw.split(",") if s.strip())


def decide(hub_dir: Path, ident, permission: str,
           surfaces: "frozenset[str] | None" = None) -> tuple[bool, str]:
    """The ONE access decision, used by both the invocation guard and MCP tool
    discovery: surface gate first, then Casbin `can()` once this hub is
    authoritative, else the legacy group check.

    FAIL CLOSED: once Casbin is (or might be) authoritative, any error
    determining or evaluating it denies — it never silently drops to legacy
    groups, which after cutover would be a bypass."""
    if surfaces is None:
        surfaces = _allowed_surfaces()
    pre_allowed, reason = is_allowed(ident, permission, allowed_surfaces=surfaces, can=lambda: True)
    if not pre_allowed:
        return False, reason
    hub_dir = Path(hub_dir)
    hub_name = hub_dir.name
    from . import store_for

    try:
        gs = store_for(hub_dir)
        authoritative = gs.is_authoritative(hub_name)
        if gs.is_suspended(getattr(ident, "user", None) or ""):
            return False, "blocked"
    except Exception:  # noqa: BLE001 — can't determine authority -> deny, don't guess
        log.exception("access: store unavailable for %s; denying", hub_name)
        return (False, "store-error")
    if authoritative:
        subject = getattr(ident, "user", None) or ""
        try:
            allowed = gs.can(subject, hub_name, permission)
        except Exception:  # noqa: BLE001 — authoritative but errored -> deny, never legacy
            log.exception("access: can() failed for %s; denying", hub_name)
            return (False, "store-error")
        return is_allowed(ident, permission, allowed_surfaces=surfaces, can=lambda: allowed)
    return is_allowed(ident, permission, allowed_surfaces=surfaces)


def guard_tool(ft: FunctionTool, permission: str, hub_dir: Path) -> FunctionTool:
    """Return a guarded copy of `ft` that enforces `permission`.

    The original is left untouched (`dataclasses.replace` copies it). The
    decision is read from the per-request identity at call time, so one guarded
    instance built at boot serves every user correctly.
    """
    surfaces = _allowed_surfaces()
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
                f"[access denied: '{ft.name}' requires the '{permission}' "
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

    return dataclasses.replace(ft, on_invoke_tool=_guarded_invoke, is_enabled=_is_enabled)


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
