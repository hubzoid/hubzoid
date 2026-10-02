# Hubzoid access management. Apache-2.0 licensed like the rest of the repository.
"""A caller's groups: hub-owned labels that describe a person and grant nothing.

Access is decided only by grants in the Console (``GrantStore.can``). Groups
are context the agent can see, from two hub-owned sources:

  * Roster groups (``identity/access.{csv,py}``), keyed by email. An email
    absent from the roster contributes nothing.
  * Header groups (``X-Hubzoid-Groups``), supplied by a trusted front or
    surface resolver (the inbound bridge carries the roster's groups here).

Every source degrades to the empty set on any failure.
"""
from __future__ import annotations

from .resolver import roster_for


def effective_groups(hub_dir, *, email, surface="owui", header_groups=None):
    """Union a caller's group sources into a set of names.

    ``header_groups`` may be a comma-separated string (as the bridge forwards
    it) or an iterable of names. ``surface`` is accepted for future
    source/surface policy. Normalization/dedup is the caller's
    (``Identity.make``).
    """
    groups: "set[str]" = set()
    if email and hub_dir is not None:
        try:
            roster = roster_for(hub_dir)
            if roster is not None:
                groups |= set(roster.groups_for_email(email))
        except Exception:  # noqa: BLE001 — context only; never fail a request
            pass
    groups |= _header_set(header_groups)
    return groups


def _header_set(header_groups) -> "set[str]":
    if not header_groups:
        return set()
    if isinstance(header_groups, str):
        return {g.strip() for g in header_groups.split(",") if g.strip()}
    return {str(g).strip() for g in header_groups if str(g).strip()}
