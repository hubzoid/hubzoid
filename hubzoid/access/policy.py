# Hubzoid access management. Apache-2.0 licensed like the rest of the repository.
"""The access decision. Pure functions, no I/O, trivially testable.

A restricted tool requires a permission (its file's normalized stem). A caller
is allowed to run it only when both hold:

  1. the caller's surface may reach restricted tools at all, and
  2. the access store grants the caller that permission in the hub.

Surfaces that do not carry a per-person verified login (slack, telegram) are
not in the allowed set, so a restricted door is never reachable from them,
whatever their grants. That is the "Slack cannot use restricted tools" rule,
enforced rather than assumed. Scheduled workflows run on the `workflow` surface
as their own service identity and reach only the restricted tools they were
granted (see DEFAULT_RESTRICTED_SURFACES).
"""
from __future__ import annotations

from .identity import Identity, normalize

# Surfaces that may reach restricted tools. Open WebUI is the verified-login
# surface; `web`/`api` are aliases for direct authenticated callers; `mcp` is
# the hosted MCP server, where the caller authenticated with their own OWUI
# API key (a per-person verified login, unlike Slack's shared bot token).
# Slack and Telegram are deliberately absent. Override per
# deployment with HUBZOID_RESTRICTED_SURFACES (comma-separated COMPLETE list;
# it replaces this default), read in guard.py.
#
# Slack, when SLACK_IDENTITY_MAPPING is on, arrives as two distinct surfaces:
# `slack-dm` (1:1 DM / assistant sidebar — a single human, safe to opt in) and
# `slack-channel` (a shared thread whose many authors are flattened into one
# prompt but answered under the @mentioner's identity — a confused-deputy risk
# that must NEVER be opted in).
# `workflow` is a trusted surface: a scheduled workflow runs as its own service
# identity (`workflow:<name>`, a per-workflow subject granted like a person), not
# a shared/anonymous sender — so it may reach a restricted tool it was granted.
DEFAULT_RESTRICTED_SURFACES = frozenset({"owui", "web", "api", "mcp", "workflow"})


def is_allowed(
    identity: Identity,
    permission: str,
    *,
    allowed_surfaces: frozenset[str] = DEFAULT_RESTRICTED_SURFACES,
    can,
) -> tuple[bool, str]:
    """Return (allowed, reason). `reason` is a short tag for the audit log.

    An empty/blank permission means the tool is not actually restricted, so it
    is always allowed. This lets callers pass a tool through the same gate
    without special-casing the unrestricted majority.

    The **surface gate always runs first** (the confused-deputy rule): a caller
    on a surface not in `allowed_surfaces` is denied before any permission check,
    so a grant is necessary but never sufficient.

    `can` (a zero-arg callable returning bool) is the permission decision:
    the access store's ``can(subject, hub, permission)``.
    """
    perm = normalize(permission)
    if not perm:
        return True, "unrestricted"
    if identity.is_anonymous:
        return False, "anonymous"
    if identity.surface not in allowed_surfaces:
        return False, f"surface:{identity.surface}"
    return (True, "grant") if can() else (False, "no-grant")
