# Hubzoid admin portal. MIT licensed like the rest of the repository.
"""The admin portal: a read-mostly JSON API + a static React SPA.

Five screens (Overview, Workflows, Access, Permissions, Audit), **view-only
except Access**, surfacing only what the access store, DBOS, and the audit log
already hold. Served by the bridge's FastAPI at `/portal`; the JSON API is under
`/portal/api`.

Auth is the same OWUI/OIDC session as chat, resolved by an injected
`admin_resolver(request) -> PortalAdmin | None`. The production resolver
validates the OWUI session server-side and strips any inbound identity header
(so a browser can't assert its own identity); a dev resolver keys off an env
var. Either way, entry is gated by `can(subject, *, manage_access)`.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from .access import store_for
from .access.store import MANAGE_ACCESS, ORG, USE_HUB, LastAdminError

log = logging.getLogger("hubzoid.portal")

_PROD_HINTS = ("prod", "_prod", "prod_", "datadog")


def _truthy_env(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


@dataclass
class PortalAdmin:
    subject: str
    is_org_admin: bool
    manageable: list[str]           # hubs this admin can edit


def _hub_perms(hub_dir: Path) -> list[str]:
    """The permission set for a hub: use_hub + manage_access + its restricted
    function stems."""
    from .access.loader import load_restricted

    perms = {USE_HUB, MANAGE_ACCESS}
    try:
        for _ft, perm in load_restricted(hub_dir):
            perms.add(perm)
    except Exception:  # noqa: BLE001
        pass
    return sorted(perms)


def _is_prod(perm: str) -> bool:
    p = perm.lower()
    return any(h in p for h in _PROD_HINTS)


def default_admin_resolver(hub_dir: Path) -> Callable[[Request], "PortalAdmin | None"]:
    """Resolve the portal admin from the request.

    Dev: `HUBZOID_PORTAL_DEV_USER=<subject>` trusts that subject (local only).
    Prod: validate the OWUI session cookie via OWUI `GET /api/v1/auths/` and use
    the verified email — never a client-sent identity header. Gated by
    `can(subject, *, manage_access)` (org) or any hub's manage_access.
    """
    hub_dir = Path(hub_dir)

    def resolve(request: Request) -> "PortalAdmin | None":
        # The dev override is a deliberate TWO-part opt-in: both
        # HUBZOID_PORTAL_DEV=1 AND HUBZOID_PORTAL_DEV_USER must be set. It bypasses
        # the OWUI session, so it is strictly for `hubzoid run` local development
        # and MUST NOT be set on any deployment with a public edge (the edge
        # proxies over loopback, so a client-IP check can't distinguish local from
        # remote). Everywhere else, identity comes only from a verified OWUI
        # session.
        subject = ""
        dev = (os.environ.get("HUBZOID_PORTAL_DEV_USER") or "").strip()
        if dev and _truthy_env("HUBZOID_PORTAL_DEV"):
            log.warning(
                "portal: HUBZOID_PORTAL_DEV is ON — trusting dev user %r without an "
                "OWUI session. NEVER set this on a public deployment.", dev
            )
            subject = dev
        if not subject:
            subject = _verify_owui_session(request)
        if not subject:
            return None
        gs = store_for(hub_dir)
        org = gs.can(subject, ORG, MANAGE_ACCESS)
        manageable = [
            h for h in _known_hubs(hub_dir, gs)
            if org or gs.can(subject, h, MANAGE_ACCESS)
        ]
        if not org and not manageable:
            return None
        return PortalAdmin(subject=subject, is_org_admin=org, manageable=manageable)

    return resolve


def _is_loopback(request: Request) -> bool:
    host = getattr(request.client, "host", "") if request.client else ""
    return host in ("127.0.0.1", "::1", "localhost")


def _check_same_origin(request: Request) -> None:
    """Reject a cross-site mutation. The session cookie is ambient, so a POST
    must come from our own origin (Origin or Referer host == request host)."""
    from urllib.parse import urlparse

    origin = request.headers.get("origin") or request.headers.get("referer") or ""
    parsed = urlparse(origin)
    # Require a real http/https origin with an authority — 'null', an opaque
    # origin, or a missing header is rejected (it can't be proven same-origin).
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(status_code=403, detail="missing or invalid Origin on a mutation")
    req_host = request.headers.get("host", "")
    if not req_host or parsed.netloc != req_host:
        raise HTTPException(status_code=403, detail="cross-origin request refused")


def _verify_owui_session(request: Request) -> str:
    """Validate the viewer's OWUI session cookie server-side and return the
    verified email, or '' — never trusting a client-sent identity header."""
    token = request.cookies.get("token") or ""
    if not token:
        return ""
    base = os.environ.get("OWUI_INTERNAL_URL") or os.environ.get("WEBUI_URL")
    if not base:
        return ""
    try:
        import httpx

        r = httpx.get(
            f"{base.rstrip('/')}/api/v1/auths/",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5.0,
        )
        if r.status_code == 200:
            return (r.json().get("email") or "").strip().lower()
    except Exception:  # noqa: BLE001
        log.warning("portal: OWUI session verification failed")
    return ""


def _known_hubs(hub_dir: Path, gs) -> list[str]:
    """Hubs the portal knows about: this deployment's hub + any hub with grants."""
    hubs = {hub_dir.name}
    for _subj, hub, _perm in gs.list_grants():
        if hub and hub != ORG:
            hubs.add(hub)
    return sorted(hubs)


def build_router(hub_dir, admin_resolver: Callable[[Request], "PortalAdmin | None"] | None = None) -> APIRouter:
    hub_dir = Path(hub_dir)
    resolver = admin_resolver or default_admin_resolver(hub_dir)
    router = APIRouter(prefix="/portal/api", tags=["portal"])

    def require_admin(request: Request) -> PortalAdmin:
        admin = resolver(request)
        if admin is None:
            raise HTTPException(status_code=403, detail="not an admin")
        return admin

    def _require_manage(admin: PortalAdmin, hub: str) -> None:
        if not (admin.is_org_admin or hub in admin.manageable):
            raise HTTPException(status_code=403, detail=f"cannot manage {hub}")

    def _require_view(admin: PortalAdmin, hub: str) -> None:
        # a hub admin may only read the hubs they manage; an org admin, all.
        if not (admin.is_org_admin or hub in admin.manageable):
            raise HTTPException(status_code=403, detail=f"cannot view {hub}")

    def _reject_reserved(subject: str, hub: str, perm: str, admin: PortalAdmin) -> None:
        # The reserved wildcard subject '*' and org domain '*' are not general
        # grant targets from the portal: only org admins, and only for the
        # intended combos (public use_hub; org-scoped manage_access).
        if subject == "*":
            if not (admin.is_org_admin and perm.lower() == USE_HUB and hub != ORG):
                raise HTTPException(403, "the wildcard subject is only for public use_hub (org admin)")
        if hub == ORG and perm.lower() != MANAGE_ACCESS:
            raise HTTPException(403, "the org domain only carries manage_access")

    @router.get("/me")
    def me(admin: PortalAdmin = Depends(require_admin)):
        return {
            "subject": admin.subject,
            "org_admin": admin.is_org_admin,
            "manageable": admin.manageable,
        }

    @router.get("/hubs")
    def hubs(admin: PortalAdmin = Depends(require_admin)):
        gs = store_for(hub_dir)
        out = []
        for h in _known_hubs(hub_dir, gs):
            if admin.is_org_admin or h in admin.manageable:
                out.append({"key": h, "name": h, "perms": _hub_perms(hub_dir)})
        return {"hubs": out}

    @router.get("/permissions")
    def permissions(hub: str, admin: PortalAdmin = Depends(require_admin)):
        _require_view(admin, hub)
        return {
            "hub": hub,
            "permissions": [
                {"permission": p, "prod": _is_prod(p)} for p in _hub_perms(hub_dir)
            ],
        }

    @router.get("/access")
    def access(hub: str, admin: PortalAdmin = Depends(require_admin)):
        _require_view(admin, hub)
        gs = store_for(hub_dir)
        # group grants by subject for this hub (+ the wildcard subject)
        rows: dict[str, dict] = {}
        for subject, dom, perm in gs.list_grants(hub):
            r = rows.setdefault(subject, {"subject": subject, "perms": [],
                                          "kind": "service" if subject.startswith("workflow:") else "person"})
            r["perms"].append(perm)
        for r in rows.values():
            r["center"] = gs.get_attr(hub, r["subject"], "center")
        return {
            "hub": hub,
            "editable": admin.is_org_admin or hub in admin.manageable,
            "permissions": _hub_perms(hub_dir),
            "rows": sorted(rows.values(), key=lambda r: r["subject"]),
        }

    @router.post("/access/grant")
    def grant(request: Request, admin: PortalAdmin = Depends(require_admin),
              payload: dict = Body(...)):
        _check_same_origin(request)
        subject = (payload.get("subject") or "").strip()
        hub = (payload.get("hub") or "").strip()
        perm = (payload.get("permission") or "").strip()
        if not subject or not hub or not perm:
            raise HTTPException(400, "subject, hub, permission required")
        # only org admins may grant manage_access
        if perm.lower() == MANAGE_ACCESS and not admin.is_org_admin:
            raise HTTPException(403, "only org admins can grant manage_access")
        _reject_reserved(subject, hub, perm, admin)
        _require_manage(admin, hub)
        store_for(hub_dir).grant(subject, hub, perm, actor=admin.subject)
        return {"ok": True}

    @router.post("/access/revoke")
    def revoke(request: Request, admin: PortalAdmin = Depends(require_admin),
               payload: dict = Body(...)):
        _check_same_origin(request)
        subject = (payload.get("subject") or "").strip()
        hub = (payload.get("hub") or "").strip()
        perm = (payload.get("permission") or "").strip()
        if perm.lower() == MANAGE_ACCESS and not admin.is_org_admin:
            raise HTTPException(403, "only org admins can revoke manage_access")
        _require_manage(admin, hub)
        try:
            store_for(hub_dir).revoke(subject, hub, perm, actor=admin.subject)
        except LastAdminError as e:
            raise HTTPException(409, str(e))
        return {"ok": True}

    @router.get("/workflows")
    def workflows(admin: PortalAdmin = Depends(require_admin)):
        try:
            from .workflows import runtime as wf

            return {"workflows": [
                {"name": w.name, "schedule": w.schedule, "timezone": w.timezone}
                for w in wf.registry()
            ]}
        except Exception:  # noqa: BLE001
            return {"workflows": []}

    @router.get("/audit")
    def audit(limit: int = 50, user: str = None, denied: bool = False,
              admin: PortalAdmin = Depends(require_admin)):
        from .access import audit as auditlib

        rows = auditlib.read(hub_dir, limit=limit, user=user,
                             decision=("deny" if denied else None))
        return {"rows": rows}

    @router.get("/access-changes")
    def access_changes(limit: int = 100, admin: PortalAdmin = Depends(require_admin)):
        """Grant/revoke change events (who changed whose access), newest first."""
        return {"rows": store_for(hub_dir).read_access_audit(limit)}

    @router.get("/overview")
    def overview(admin: PortalAdmin = Depends(require_admin)):
        gs = store_for(hub_dir)
        grants = gs.list_grants()
        subjects = {s for s, _h, _p in grants}
        return {
            "hubs": len(_known_hubs(hub_dir, gs)),
            "grants": len(grants),
            "people": len(subjects),
            "authoritative": gs.is_authoritative(),
        }

    return router


def mount_portal(app, hub_dir) -> None:
    """Mount the portal API + the built static SPA (if present) on the bridge."""
    hub_dir = Path(hub_dir)
    app.include_router(build_router(hub_dir))
    dist = Path(__file__).parent / "portal_dist"
    if dist.is_dir():
        from fastapi.staticfiles import StaticFiles

        app.mount("/portal", StaticFiles(directory=str(dist), html=True), name="portal")
        log.info("portal: serving SPA from %s", dist)
    else:
        log.info("portal: API mounted; static SPA not built (no portal_dist/)")
