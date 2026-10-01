# Hubzoid access management. Apache-2.0 licensed like the rest of the repository.
"""The one place a caller's groups are assembled from every source.

A person can be granted a group in more than one store, and which stores are
consulted depends on the surface. This function is the single, readable rule:

  * Hubzoid groups (``hz_groups``, the Console's Groups screen) — the
    admin-managed store in the web app mode (``HUBZOID_UI`` unset or
    ``hubzoid``). Their NAMES take the place Open WebUI group names had: an
    agent whose access is not yet managed in the Console still reads a group
    name as the restricted-tool permission it confers. A blocked person has
    none.
  * Open WebUI groups (``webui.db``) — the admin-managed store in the legacy
    Open WebUI mode (``HUBZOID_UI=openwebui``). Consulted on any surface that
    forwards a verified email.
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
That check stays OWUI-only in ``mcp_oauth.HubOAuth``.
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
        groups |= _admin_groups(hub_dir, email)
        roster = roster_for(hub_dir)
        if roster is not None:
            groups |= set(roster.groups_for_email(email))

    groups |= _header_set(header_groups)
    return groups


def _admin_groups(hub_dir, email) -> "set[str]":
    """The administrator-managed groups: Hubzoid groups in the web app mode,
    Open WebUI groups in the legacy mode. Empty on any failure."""
    from ..appmode import is_legacy

    try:
        legacy = is_legacy(hub_dir)
    except Exception:  # noqa: BLE001 — unknown mode: no admin groups (deny)
        return set()
    if legacy:
        return set(owui_groups.resolve_groups(hub_dir, email))
    from ..groups import names_for

    return names_for(hub_dir, email)


def _header_set(header_groups) -> "set[str]":
    if not header_groups:
        return set()
    if isinstance(header_groups, str):
        return {g.strip() for g in header_groups.split(",") if g.strip()}
    return {str(g).strip() for g in header_groups if str(g).strip()}
