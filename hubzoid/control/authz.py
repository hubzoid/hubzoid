"""Platform permission keys and who holds them today.

The 2.0 permission keys (``<area>.<action>``, conventions section 8) are named
here so every control-plane route asks for the exact capability it needs. The
groups and feature-permission registry that will hold them is separate work;
until it lands, each key maps onto today's grants:

* ``deployment`` keys need the organization ``manage_access`` grant (an org
  administrator), or the local owner when sign-in is off,
* ``hub`` keys need ``manage_access`` on that hub, or the org grant,
* ``any_hub`` keys need ``manage_access`` on at least one hub.

That mapping lives only in this module, so replacing it later changes no route.
A hidden button never stands in for any of this: every route calls ``require``.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import HTTPException, Request

log = logging.getLogger("hubzoid.control.authz")

#: key -> scope
CAPABILITIES: dict[str, str] = {
    "chat_config.read": "deployment",
    "chat_config.manage": "deployment",
    "branding.read": "deployment",
    "branding.manage": "deployment",
    "branding.activate": "deployment",
    "admin.access": "any_hub",
    "admin.config.read": "deployment",
    "admin.config.write": "deployment",
    "admin.diagnostics.read": "hub",
    "admin.diagnostics.run": "hub",
    "admin.diagnostics.network": "hub",
    "admin.diagnostics.export": "hub",
    "parity.read": "deployment",
    "parity.write": "deployment",
    "parity.review": "deployment",
    "chat.feedback.read": "hub",
    "chat.notifications.manage": "hub",
    "chat.share.manage": "hub",
    "authoring.draft": "hub",
    "authoring.publish": "hub",
    "channels.manage": "hub",
    "channels.inspect": "hub",
    "intake.read": "hub",
    "intake.manage": "hub",
    "hitl.manage": "hub",
    "hitl.inspect": "hub",
}


@dataclass(frozen=True)
class Principal:
    """A verified person and what they manage."""

    id: str
    email: str
    name: str = ""
    org_admin: bool = False
    hubs: tuple[str, ...] = field(default_factory=tuple)
    method: str = ""

    def can(self, capability: str, hub: str | None = None) -> bool:
        scope = CAPABILITIES.get(capability)
        if scope is None:
            raise KeyError(f"unknown capability {capability!r}")
        if self.org_admin:
            return True
        if scope == "deployment":
            return False
        if scope == "any_hub":
            return bool(self.hubs)
        return bool(hub) and hub in self.hubs

    def capabilities(self, hub: str | None = None) -> list[str]:
        return sorted(k for k in CAPABILITIES if self.can(k, hub))


def denied(capability: str) -> HTTPException:
    return HTTPException(403, detail={
        "code": "permission_denied",
        "message": (f"You need the {capability} permission for this. Ask the owner of this "
                    "deployment to grant it."),
        "capability": capability})


def principal_for(user, hub_dir: Path) -> Principal:
    """The principal of a signed-in ``hubzoid.auth.AuthUser``. Raises
    HTTPException(503) when the access store can't answer (fail closed)."""
    from ..access.identity import normalize
    from ..access.service import AccessService, Actor, Denied

    email = normalize(user.email)
    try:
        scope = AccessService(Path(hub_dir)).scope(Actor(email, "console", "session"))
    except Denied as exc:
        raise HTTPException(exc.status, detail={"code": "unavailable", "message": exc.message})
    except Exception as exc:  # noqa: BLE001 - fail closed
        log.exception("authz: scope check failed")
        raise HTTPException(503, detail={"code": "unavailable",
                                         "message": "Access check unavailable. Try again shortly.",
                                         "retryable": True}) from exc
    return Principal(id=user.id, email=email, name=getattr(user, "name", "") or "",
                     org_admin=bool(scope.org_admin), hubs=tuple(scope.hubs),
                     method=getattr(user, "method", "") or "")


async def principal(request: Request, hub_dir: Path) -> Principal:
    """The signed-in caller as a principal (401 when signed out)."""
    from .. import auth

    user = await asyncio.to_thread(auth.require_user, request, Path(hub_dir))
    return await asyncio.to_thread(principal_for, user, Path(hub_dir))


def require(p: Principal, capability: str, hub: str | None = None) -> Principal:
    if not p.can(capability, hub):
        raise denied(capability)
    return p


async def require_request(request: Request, hub_dir: Path, capability: str,
                          hub: str | None = None, *, mutation: bool = False) -> Principal:
    """Sign-in, same-origin for mutations, then the capability."""
    if mutation:
        same_origin(request)
    return require(await principal(request, hub_dir), capability, hub)


def same_origin(request: Request) -> None:
    """A cookie is ambient, so a state change must come from our own origin."""
    from ..access import session

    try:
        session.require_same_origin(request)
    except HTTPException as exc:
        if isinstance(exc.detail, dict):
            raise
        raise HTTPException(exc.status_code, detail={
            "code": "cross_origin",
            "message": "This request did not come from this site. Reload the page and try again.",
        }) from exc
