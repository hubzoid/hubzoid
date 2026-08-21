# Hubzoid Enterprise · access management. Production use requires a license
# with the "access" entitlement; free to run for development. See LICENSING.md.
"""The one place a caller's groups are assembled from every source.

A person can be granted a group in more than one store, and which stores are
consulted depends on the surface. This function is the single, readable rule:

  * Open WebUI groups (``webui.db``) — the admin-managed store. Consulted on any
    surface that forwards a verified email.
  * Roster groups (``identity/access.{csv,py}``) — the hub-owned store, keyed by
    email. ADDITIVE, never a gate: an email absent from the roster contributes
    nothing, so OWUI-only users are never locked out. This is what unifies the
    WhatsApp and Open WebUI surfaces — the same email resolves the same groups
    whichever door it comes through.
  * Header groups (``X-Hubzoid-Groups``) — supplied by a trusted front / surface
    resolver (the inbound bridge already carries the roster's groups here).

Every source degrades to the empty set on any failure, so the fail-closed
default is preserved: a lookup that goes wrong denies, it never grants.

Deliberately NOT used for the MCP front door (``MCP_ACCESS_GROUP``), which is an
OWUI-admin-managed tenant boundary — the roster must not be able to open it.
That check stays OWUI-only in ``mcp_server._build_verifier``.
"""
from __future__ import annotations

from . import owui_groups
from .resolver import roster_for


def effective_groups(hub_dir, *, email, surface="owui", header_groups=None):
    """Union a caller's group sources into a set of normalized names.

    ``header_groups`` may be a comma-separated string (as the bridge forwards
    it) or an iterable of names. ``surface`` is accepted for future
    source/surface policy; today every listed source applies to every
    email-carrying surface. Normalization/dedup is the caller's (``Identity.make``).
    """
    groups: "set[str]" = set()

    if email and hub_dir is not None:
        groups |= set(owui_groups.resolve_groups(hub_dir, email))
        roster = roster_for(hub_dir)
        if roster is not None:
            groups |= set(roster.groups_for_email(email))

    groups |= _header_set(header_groups)
    return groups


def _header_set(header_groups) -> "set[str]":
    if not header_groups:
        return set()
    if isinstance(header_groups, str):
        return {g.strip() for g in header_groups.split(",") if g.strip()}
    return {str(g).strip() for g in header_groups if str(g).strip()}
