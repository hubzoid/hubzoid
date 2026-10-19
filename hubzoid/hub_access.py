"""Who is asking, for the hub-scoped Console APIs (secrets, settings, doctor).

``viewer(request, hub_dir)`` answers with the caller's email and whether they
are an organization administrator (an administrator account with
organization-wide Manage access, as the Console decides it) or an
administrator of this hub. With sign-in off, the local owner on this machine
is both, and only requests addressed to this machine are answered.

These stand in for the platform permission keys (``secrets.manage``,
``settings.manage``) until the shared permission registry exists.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fastapi import HTTPException, Request

from .access.identity import normalize
from .assertions import hub_key
from .chat.common import error


@dataclass(frozen=True)
class Viewer:
    email: str
    org_admin: bool
    hub_admin: bool


def _trusted_host(request: Request) -> None:
    from . import appmode
    from .connectors import http as net
    from .connectors import oauth_flow

    if appmode.allowed_origins():
        return
    if not net.is_loopback_url(oauth_flow.request_origin(request)):
        raise error(403, "untrusted_host", "Open Hubzoid at its local address, or set "
                                           "HUBZOID_PUBLIC_URL to the address people use.")


def viewer(request: Request, hub_dir: Path) -> Viewer:
    from . import appmode
    from .access.service import AccessService, Actor, Denied
    from .auth import LOCAL_OWNER_EMAIL
    from .connectors.routes import signed_in

    hub_dir = Path(hub_dir)
    if not appmode.is_openwebui(hub_dir) and not appmode.auth_enabled(hub_dir):
        _trusted_host(request)
    user = signed_in(request, hub_dir)
    if user is None:
        raise error(401, "unauthenticated", "Sign in to continue.")
    email = normalize(user.email)
    if not appmode.auth_enabled(hub_dir) and email == LOCAL_OWNER_EMAIL:
        return Viewer(email, True, True)
    try:
        scope = AccessService(hub_dir).scope(Actor(email, "console", "session"))
    except Denied as exc:
        raise error(exc.status, exc.code, exc.message) from None
    org = scope.org_admin and (appmode.is_openwebui(hub_dir) or getattr(user, "is_admin", False))
    return Viewer(email, org, org or hub_key(hub_dir) in scope.hubs)


def hub_admin(request: Request, hub_dir: Path) -> Viewer:
    """This hub's administrators and organization administrators. Anyone else
    gets 404, as if the screen did not exist."""
    who = viewer(request, hub_dir)
    if not who.hub_admin:
        raise error(404, "not_found", "Not found.")
    return who


def same_origin(request: Request) -> None:
    """Refuse a write that does not come from this site's own pages."""
    from .access.session import require_same_origin

    try:
        require_same_origin(request)
    except HTTPException:
        raise error(403, "cross_origin", "This request must come from this site's own pages.") from None


def request_id(request: Request) -> str | None:
    return (request.headers.get("x-request-id") or "").strip()[:128] or None
