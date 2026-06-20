# Hubzoid Enterprise · access management.
# Source-available and free to run for development and testing; production use
# requires a license key with the "access" entitlement. See LICENSING.md.
# This is a notice, not a gate: the feature runs on the community tier too.
"""Wrap a restricted FunctionTool so the gate runs in code before it executes.

Two layers, exactly as the design says, and both backends get them because both
factories build from the same FunctionTool registry:

  * is_enabled  -> hides the tool from a caller who lacks the permission. The
                   OpenAI Agents SDK evaluates this per run, so the agent is
                   never even shown a door it cannot open (no wasted turn, no
                   leak of the tool's name and schema to the unauthorized).
  * on_invoke   -> re-checks at call time and fails closed, writing the decision
                   to the audit log. This is the wall: it holds even if the tool
                   is reached another way (a prompt injection naming it, the
                   Claude path which does not consult is_enabled, or a test).

`apply` is the one entry point the factories call. With no `restricted/` folder
it returns the registry unchanged, so nothing about an existing hub moves.
"""
from __future__ import annotations

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


def _notice_license() -> None:
    """Inform, do not block, when access management runs unlicensed.

    Soft open-core: the feature runs fully on the community tier; this is a
    heads-up, not a gate, and a valid ``access`` license silences it (no output
    on success). To switch from informing to enforcing later, change the warning
    to a raise here.
    """
    from .. import licensing

    if not licensing.load_license().has_feature("access"):
        log.warning(
            "Access management (restricted/) is a Hubzoid Enterprise feature; "
            "production use needs a license. See LICENSING.md."
        )


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
        allowed, reason = is_allowed(ident, permission, allowed_surfaces=surfaces)
        audit.record(
            hub_dir, user=ident.user, surface=ident.surface, tool=ft.name,
            decision=("allow" if allowed else "deny"), reason=reason,
        )
        if not allowed:
            return (
                f"[access denied: '{ft.name}' requires the '{permission}' "
                "permission, which the current user does not have. "
                "This attempt was logged.]"
            )
        return await original_invoke(ctx, input_str)

    def _is_enabled(*_args, **_kwargs) -> bool:
        # The SDK calls this with (run_context, agent); we only need the
        # request-scoped identity, so accept anything and ignore it.
        allowed, _ = is_allowed(current_identity(), permission, allowed_surfaces=surfaces)
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
    _notice_license()  # inform that this is an Enterprise feature; never blocks
    out = dict(registry)
    for ft, permission in restricted:
        out[ft.name] = guard_tool(ft, permission, hub_dir)
    log.info(
        "access: %d restricted tool(s) under %d permission(s) loaded",
        len(restricted), len({p for _, p in restricted}),
    )
    return out
