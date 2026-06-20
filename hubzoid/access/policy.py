# Hubzoid Enterprise · access management. Production use requires a license
# with the "access" entitlement; free to run for development. See LICENSING.md.
"""The access decision. Pure functions, no I/O, trivially testable.

A restricted tool requires a permission (its file's normalized stem). A caller
is allowed to run it only when both hold:

  1. the caller's surface may reach restricted tools at all, and
  2. the caller is in a group whose normalized name equals the permission.

Surfaces that do not carry a per-person verified login (slack, telegram,
scheduled) are not in the allowed set, so a restricted door is never reachable
from them, regardless of groups. That is the "Slack cannot use restricted tools"
rule, enforced rather than assumed.
"""
from __future__ import annotations

from .identity import Identity, normalize

# Surfaces that may reach restricted tools. Open WebUI is the verified-login
# surface; `web`/`api` are aliases for direct authenticated callers. Slack,
# Telegram and scheduled runs are deliberately absent. Override per deployment
# with HUBZOID_RESTRICTED_SURFACES (comma-separated), read in guard.py.
DEFAULT_RESTRICTED_SURFACES = frozenset({"owui", "web", "api"})


def is_allowed(
    identity: Identity,
    permission: str,
    *,
    allowed_surfaces: frozenset[str] = DEFAULT_RESTRICTED_SURFACES,
) -> tuple[bool, str]:
    """Return (allowed, reason). `reason` is a short tag for the audit log.

    An empty/blank permission means the tool is not actually restricted, so it
    is always allowed. This lets callers pass a tool through the same gate
    without special-casing the unrestricted majority.
    """
    perm = normalize(permission)
    if not perm:
        return True, "unrestricted"
    if identity.is_anonymous:
        return False, "anonymous"
    if identity.surface not in allowed_surfaces:
        return False, f"surface:{identity.surface}"
    if perm in identity.groups:
        return True, "group"
    return False, "no-group"
