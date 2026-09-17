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

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from pydantic import BaseModel, Field, ConfigDict
from . import deployment
from .access.identity import normalize

from .access import store_for
from .access.store import MANAGE_ACCESS, ORG, USE_HUB, LastAdminError

log = logging.getLogger("hubzoid.portal")


def _truthy_env(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


@dataclass
class PortalAdmin:
    subject: str
    is_org_admin: bool
    manageable: list[str]


class GrantRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    subject: str = Field(min_length=1, max_length=320)
    hub: str = Field(min_length=1, max_length=200)
    permission: str = Field(min_length=1, max_length=200)


class PersonRequest(BaseModel):
    subject: str = Field(min_length=1, max_length=320)
    suspended: bool = True


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
                "OWUI session. NEVER set this on a public deployment.",
                dev,
            )
            subject = dev
        if not subject:
            subject = _verify_owui_session(request, hub_dir)
        if not subject:
            return None
        gs = store_for(hub_dir)
        org = gs.can(subject, ORG, MANAGE_ACCESS)
        manageable = [
            h
            for h in _known_hubs(hub_dir, gs)
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
        raise HTTPException(
            status_code=403, detail="missing or invalid Origin on a mutation"
        )
    req_host = request.headers.get("host", "")
    if not req_host or parsed.netloc != req_host:
        raise HTTPException(status_code=403, detail="cross-origin request refused")


def _verify_owui_session(request: Request, hub_dir: Path | None = None) -> str:
    """Validate the viewer's OWUI session cookie server-side and return the
    verified email, or '' — never trusting a client-sent identity header."""
    token = request.cookies.get("token") or ""
    if not token:
        return ""
    base = (
        deployment.owui_url(hub_dir)
        if hub_dir
        else (os.environ.get("OWUI_INTERNAL_URL") or os.environ.get("WEBUI_URL"))
    )
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
            user = r.json()
            if user.get("role") == "pending":
                return ""
            email = normalize(user.get("email", ""))
            if hub_dir and email:
                store_for(hub_dir).upsert_identity(
                    email=email, owui_id=user.get("id"), display=user.get("name")
                )
            return email
    except Exception:  # noqa: BLE001
        log.warning("portal: OWUI session verification failed")
    return ""


def _known_hubs(hub_dir: Path, gs) -> list[str]:
    return sorted(h["key"] for h in deployment.hubs(hub_dir))


def build_router(hub_dir, admin_resolver=None) -> APIRouter:
    hub_dir = Path(hub_dir)
    resolver = admin_resolver or default_admin_resolver(hub_dir)
    router = APIRouter(prefix="/portal/api", tags=["portal"])

    def require_admin(request: Request) -> PortalAdmin:
        admin = resolver(request)
        if admin is None:
            raise HTTPException(
                403, "Sign in with an account allowed to manage agent access."
            )
        return admin

    def allowed_hubs(admin):
        return [
            h
            for h in deployment.hubs(hub_dir)
            if admin.is_org_admin or h["key"] in admin.manageable
        ]

    def require_hub(admin, hub):
        if not admin.is_org_admin and hub not in admin.manageable:
            raise HTTPException(403, f"Cannot manage {hub}")
        try:
            return deployment.hub_path(hub_dir, hub)
        except KeyError:
            raise HTTPException(404, "Hub is not registered in this deployment")

    def selected(admin, hub):
        if hub:
            require_hub(admin, hub)
            return [h for h in allowed_hubs(admin) if h["key"] == hub]
        return allowed_hubs(admin)

    @router.get("/me")
    def me(admin=Depends(require_admin)):
        return dict(
            subject=admin.subject,
            org_admin=admin.is_org_admin,
            manageable=[h["key"] for h in allowed_hubs(admin)],
        )

    @router.get("/hubs")
    def hubs(admin=Depends(require_admin)):
        gs = store_for(hub_dir)
        return {
            "hubs": [
                dict(
                    key=h["key"],
                    name=h["name"],
                    authoritative=gs.is_authoritative(h["key"]),
                )
                for h in allowed_hubs(admin)
            ]
        }

    @router.get("/permissions")
    def permissions(hub: str, admin=Depends(require_admin)):
        path = require_hub(admin, hub)
        return dict(hub=hub, permissions=deployment.permission_catalog(path))

    @router.get("/access")
    def access(
        hub: str,
        q: str = "",
        offset: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=200),
        admin=Depends(require_admin),
    ):
        path = require_hub(admin, hub)
        gs = store_for(hub_dir)
        rows = {}
        for subject, domain, perm in gs.list_grants():
            if domain == hub or (domain == ORG and perm == MANAGE_ACCESS):
                row = rows.setdefault(
                    subject,
                    dict(
                        subject=subject,
                        perms=[],
                        inherited=[],
                        kind="service" if subject.startswith("workflow:") else "person",
                    ),
                )
                row["perms" if domain == hub else "inherited"].append(perm)
        for subject, row in rows.items():
            row["center"] = gs.get_attr(hub, subject, "center")
            row["effective"] = sorted(gs.permissions_for(subject, hub))
            identity = gs.identity(subject) or {}
            row["display"] = identity.get("display") or subject
            row["status"] = (
                "blocked"
                if gs.is_suspended(subject)
                else (
                    "everyone"
                    if subject == "*"
                    else (
                        "service"
                        if row["kind"] == "service"
                        else (
                            "awaiting-signup"
                            if not identity.get("owui_id")
                            else (
                                "pending-approval"
                                if identity.get("pending")
                                else "active"
                            )
                        )
                    )
                )
            )
        result = [
            r
            for r in rows.values()
            if q.lower() in (r["subject"] + " " + r["display"]).lower()
        ]
        result.sort(key=lambda r: r["subject"])
        return dict(
            hub=hub,
            editable=True,
            authoritative=gs.is_authoritative(hub),
            can_manage_admins=admin.is_org_admin,
            permissions=deployment.permission_catalog(path),
            total=len(result),
            public=gs.can("__signed_in_preview__", hub, USE_HUB),
            rows=result[offset : offset + limit],
        )

    def mutate(request, payload, admin, revoke=False):
        _check_same_origin(request)
        subject, hub, perm = map(
            normalize, (payload.subject, payload.hub, payload.permission)
        )
        gs = store_for(hub_dir)
        if hub == ORG:
            if not admin.is_org_admin or perm != MANAGE_ACCESS or subject == "*":
                raise HTTPException(
                    403,
                    "Only organization admins can manage organization administrators",
                )
        else:
            path = require_hub(admin, hub)
            known = {p["permission"] for p in deployment.permission_catalog(path)}
            if perm not in known and not (
                revoke and (subject, hub, perm) in gs.list_grants(hub)
            ):
                raise HTTPException(422, "Unknown permission for this hub")
        if subject == "*" and not (admin.is_org_admin and perm == USE_HUB):
            raise HTTPException(
                403, "Only organization admins may change public hub access"
            )
        if not admin.is_org_admin and (
            perm == MANAGE_ACCESS
            or (revoke and perm == USE_HUB and gs.can(subject, hub, MANAGE_ACCESS))
        ):
            raise HTTPException(
                403, "Only organization admins may change administrator access"
            )
        try:
            if revoke:
                gs.revoke(subject, hub, perm, actor=admin.subject)
            else:
                if gs.is_suspended(subject):
                    raise HTTPException(
                        409, "Reactivate this user before granting access"
                    )
                gs.grant(subject, hub, perm, actor=admin.subject)
        except (ValueError, LastAdminError) as exc:
            raise HTTPException(409, str(exc))
        return dict(
            ok=True,
            visibility="Updates in Open WebUI within 30 seconds; use Sync now to retry.",
        )

    @router.post("/access/grant")
    def grant(request: Request, payload: GrantRequest, admin=Depends(require_admin)):
        return mutate(request, payload, admin)

    @router.post("/access/revoke")
    def revoke(request: Request, payload: GrantRequest, admin=Depends(require_admin)):
        return mutate(request, payload, admin, True)

    @router.get("/people")
    def people(
        q: str = "",
        offset: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=200),
        admin=Depends(require_admin),
    ):
        gs = store_for(hub_dir)
        scopes = {h["key"] for h in allowed_hubs(admin)}
        grants = [g for g in gs.list_grants() if admin.is_org_admin or g[1] in scopes]
        visible = {g[0] for g in grants}
        rows = []
        for person in gs.identities():
            sub = person["subject"]
            if not admin.is_org_admin and sub not in visible:
                continue
            if q.lower() not in (sub + " " + (person["display"] or "")).lower():
                continue
            person["blocked"] = gs.is_suspended(sub)
            person["organization_admin"] = gs.can(sub, ORG, MANAGE_ACCESS)
            person["access"] = {h: sorted(gs.permissions_for(sub, h)) for h in scopes}
            rows.append(person)
        return {"people": rows[offset : offset + limit], "total": len(rows)}

    @router.post("/people/refresh")
    def refresh_people(request: Request, admin=Depends(require_admin)):
        _check_same_origin(request)
        if not admin.is_org_admin:
            raise HTTPException(403, "Organization admin required")
        from .access.owui import directory

        try:
            rows = directory(hub_dir)
            store_for(hub_dir).reconcile_accounts(
                [
                    dict(
                        id=r["owui_id"],
                        email=r["email"],
                        name=r["display"],
                        role=r["role"],
                    )
                    for r in rows
                ]
            )
            return {"ok": True, "count": len(rows)}
        except Exception:
            log.exception("OWUI directory refresh failed")
            raise HTTPException(
                503, "Account refresh failed. Check OWUI service credentials and logs."
            )

    @router.post("/people/block")
    def block(request: Request, payload: PersonRequest, admin=Depends(require_admin)):
        _check_same_origin(request)
        if not admin.is_org_admin:
            raise HTTPException(403, "Organization admin required")
        try:
            store_for(hub_dir).suspend(
                payload.subject, actor=admin.subject, suspended=payload.suspended
            )
        except (LastAdminError, ValueError) as exc:
            raise HTTPException(409, str(exc))
        return {"ok": True}

    @router.get("/workflows")
    def workflows(hub: str | None = None, admin=Depends(require_admin)):
        from .workflows.observe import catalog

        return {
            "workflows": [
                w for h in selected(admin, hub) for w in catalog(Path(h["path"]))
            ]
        }

    @router.get("/runs")
    def runs(
        hub: str,
        workflow: str | None = None,
        run_id: str | None = None,
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
        admin=Depends(require_admin),
    ):
        from .workflows.observe import runs as read_runs

        path = require_hub(admin, hub)
        try:
            return {
                "runs": read_runs(
                    path, name=workflow, run_id=run_id, limit=limit, offset=offset
                )
            }
        except Exception:
            log.exception("Workflow history unavailable")
            raise HTTPException(
                503,
                "Run history unavailable; check the workflow database and server logs.",
            )

    @router.get("/audit")
    def audit(
        hub: str | None = None,
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0, le=10000),
        user: str | None = None,
        denied: bool = False,
        admin=Depends(require_admin),
    ):
        from .access import audit as auditlib

        rows = [
            dict(r, hub=h["key"])
            for h in selected(admin, hub)
            for r in auditlib.read(
                Path(h["path"]),
                limit=limit + offset,
                user=user,
                decision="deny" if denied else None,
            )
        ]
        rows.sort(key=lambda r: r.get("ts", ""), reverse=True)
        return {"rows": rows[offset : offset + limit]}

    @router.get("/access-changes")
    def changes(
        hub: str | None = None,
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
        user: str | None = None,
        admin=Depends(require_admin),
    ):
        scopes = [h["key"] for h in selected(admin, hub)]
        if admin.is_org_admin and not hub:
            scopes.append(ORG)
        return {
            "rows": store_for(hub_dir).read_access_audit(
                limit, hubs=scopes, subject=user, offset=offset
            )
        }

    @router.post("/sync")
    def sync(request: Request, admin=Depends(require_admin)):
        _check_same_origin(request)
        if not admin.is_org_admin:
            raise HTTPException(403, "Organization admin required")
        from .access.reconcile import sync_owui

        return sync_owui(hub_dir)

    @router.get("/overview")
    def overview(admin=Depends(require_admin)):
        from .access.reconcile import sync_status

        gs = store_for(hub_dir)
        hs = allowed_hubs(admin)
        keys = {h["key"] for h in hs}
        grants = [g for g in gs.list_grants() if g[1] in keys]
        managed = sum(gs.is_authoritative(h) for h in keys)
        return dict(
            hubs=len(hs),
            grants=len(grants),
            people=len({g[0] for g in grants if g[0] != "*"}),
            authoritative=managed == len(hs),
            managed=managed,
            legacy=len(hs) - managed,
            visibility=sync_status(hub_dir),
        )

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
