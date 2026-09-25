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
from typing import Callable, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from pydantic import BaseModel, Field, ConfigDict
from . import deployment
from .access.identity import normalize

from .access import store_for
from .access.store import (
    MANAGE_ACCESS,
    ORG,
    USE_HUB,
    EVERYONE,
    LastAdminError,
    RevisionConflict,
)

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
    # Optional optimistic-concurrency guard: the policy revision the editor
    # loaded. If it no longer matches, another admin changed access in the
    # meantime and we refuse rather than apply an edit built on stale state.
    expected_revision: int | None = None


class ApplyOp(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    action: Literal["grant", "revoke"]
    permission: str = Field(min_length=1, max_length=200)


class ApplyRequest(BaseModel):
    """A whole change set for one subject in one hub, applied atomically."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    subject: str = Field(min_length=1, max_length=320)
    hub: str = Field(min_length=1, max_length=200)
    expected_revision: int | None = None
    operations: list[ApplyOp] = Field(min_length=1, max_length=100)


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


# ---- account state ----------------------------------------------------------
#
# The store keeps two independent block markers per subject and `is_suspended`
# ORs them: `suspended:<subject>` (an admin's explicit block, or an
# account_replaced safeguard) and `account_unavailable:<subject>` (derived from
# Open WebUI: the account is pending approval, or vanished from the directory).
# Only the first is cleared by "reactivate"; the second only clears when OWUI
# reports the account as approved/present again. The portal exposes them apart
# so the UI can tell "blocked by an admin" from "blocked by the chat app".

_UNAVAILABLE_MSG = (
    "This account is unavailable in the chat app (awaiting approval or removed). "
    "Approve or restore it in Open WebUI, then refresh accounts."
)

# A hub whose access is not yet dashboard-managed (Casbin not authoritative) is still
# governed by the chat app. Editing its access here would neither take effect nor
# survive migration, so those edits are refused (in the API, not only the UI).
_LEGACY_MSG = (
    "This agent's access is still managed in the chat app — it has not been migrated "
    "to the dashboard. Migrate the agent first; edits made here would not take effect "
    "and would be overwritten by migration."
)


def _account_flags(gs, subject: str) -> dict:
    """Read the store's two block markers separately (read-only). `blocked` is
    the OR of both and always equals `gs.is_suspended(subject)`."""
    subject = normalize(subject)
    with gs._engine.connect() as conn:  # noqa: SLF001 — read-only marker lookup
        suspended = gs._meta_get(conn, "suspended:" + subject) == "1"
        unavailable = gs._meta_get(conn, "account_unavailable:" + subject) == "1"
    return dict(
        suspended=suspended,
        account_unavailable=unavailable,
        blocked=suspended or unavailable,
    )


def _account_status(subject: str, identity: dict, flags: dict) -> str:
    """One display status per subject. Precedence: an admin block beats
    everything; then the structural kinds; then signup/approval progress; an
    OWUI-side unavailable account that is *not* pending (deleted/missing) is
    reported as `blocked` with `account_unavailable=true` alongside."""
    if flags["suspended"]:
        return "blocked"
    if subject == "*":
        return "everyone"
    if subject.startswith("workflow:"):
        return "service"
    if not identity.get("owui_id"):
        return "awaiting-signup"
    if identity.get("pending"):
        return "pending-approval"
    if flags["account_unavailable"]:
        return "blocked"
    return "active"


def _account_state(gs, subject: str, identity: dict | None = None) -> dict:
    identity = identity if identity is not None else (gs.identity(subject) or {})
    flags = _account_flags(gs, subject)
    return dict(flags, status=_account_status(normalize(subject), identity, flags))


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
        # One consistent read of (revision, every grant): the returned revision
        # describes exactly the rows below, so the editor's concurrency guard is
        # not defeated by new rows arriving under an old revision (or the reverse).
        revision, all_grants = gs.access_snapshot()
        rows = {}
        for subject, domain, perm in all_grants:
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

        def effective_for(subject: str) -> list[str]:
            # Mirrors GrantStore.permissions_for over the same snapshot: direct +
            # org-wide + public wildcard. Suspended subjects hold nothing.
            return sorted(
                {
                    p
                    for (s, h, p) in all_grants
                    if (s == subject or s == EVERYONE) and (h == hub or h == ORG)
                }
            )

        for subject, row in rows.items():
            row["center"] = gs.get_attr(hub, subject, "center")
            identity = gs.identity(subject) or {}
            row["display"] = identity.get("display") or subject
            state = _account_state(gs, subject, identity)
            row.update(state)
            # Effective access must match the enforcer: a blocked account (admin
            # suspension OR an unavailable chat account) holds nothing, though its
            # direct grants are preserved separately in `perms`.
            row["effective"] = [] if state["blocked"] else effective_for(subject)
        result = [
            r
            for r in rows.values()
            if q.lower() in (r["subject"] + " " + r["display"]).lower()
        ]
        result.sort(key=lambda r: r["subject"])
        public = any(
            s == EVERYONE and (h == hub or h == ORG) and p == USE_HUB
            for (s, h, p) in all_grants
        )
        return dict(
            hub=hub,
            # Editable only once the hub is dashboard-managed. A legacy (un-migrated)
            # hub is read-only here; its access still lives in the chat app.
            editable=gs.is_authoritative(hub),
            authoritative=gs.is_authoritative(hub),
            can_manage_admins=admin.is_org_admin,
            permissions=deployment.permission_catalog(path),
            total=len(result),
            public=public,
            revision=revision,
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
            if not gs.is_authoritative(hub):
                raise HTTPException(409, _LEGACY_MSG)
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
        if (
            payload.expected_revision is not None
            and gs.revision() != payload.expected_revision
        ):
            raise HTTPException(
                409,
                "Access changed since you loaded it — someone else edited it. "
                "Reload and review the current access before saving.",
            )
        try:
            if revoke:
                gs.revoke(subject, hub, perm, actor=admin.subject)
            else:
                flags = _account_flags(gs, subject)
                if flags["suspended"]:
                    raise HTTPException(
                        409, "Reactivate this user before granting access"
                    )
                if flags["account_unavailable"]:
                    raise HTTPException(409, _UNAVAILABLE_MSG)
                gs.grant(subject, hub, perm, actor=admin.subject)
        except (ValueError, LastAdminError) as exc:
            raise HTTPException(409, str(exc))
        return dict(ok=True, revision=gs.revision())

    @router.post("/access/grant")
    def grant(request: Request, payload: GrantRequest, admin=Depends(require_admin)):
        return mutate(request, payload, admin)

    @router.post("/access/revoke")
    def revoke(request: Request, payload: GrantRequest, admin=Depends(require_admin)):
        return mutate(request, payload, admin, True)

    @router.post("/access/apply")
    def apply(request: Request, payload: ApplyRequest, admin=Depends(require_admin)):
        """Apply one person's whole change set for a hub in a single guarded
        transaction. Atomic: either every operation applies on the expected
        revision, or none does (409 on a concurrent change)."""
        _check_same_origin(request)
        subject = normalize(payload.subject)
        hub = normalize(payload.hub)
        if hub == ORG:
            raise HTTPException(400, "Organization admin rights are changed per person, not here")
        if subject == EVERYONE:
            raise HTTPException(403, "Public access is changed with the public-access toggle")
        path = require_hub(admin, hub)
        gs = store_for(hub_dir)
        if not gs.is_authoritative(hub):
            raise HTTPException(409, _LEGACY_MSG)
        known = {p["permission"] for p in deployment.permission_catalog(path)}
        existing = set(gs.list_grants(hub))
        ops: list[tuple[str, str]] = []
        grants = False
        for op in payload.operations:
            perm = normalize(op.permission)
            if not admin.is_org_admin and (
                perm == MANAGE_ACCESS
                or (op.action == "revoke" and perm == USE_HUB and gs.can(subject, hub, MANAGE_ACCESS))
            ):
                raise HTTPException(
                    403, "Only organization admins may change administrator access"
                )
            # A removed/renamed tool can still be revoked even though it left the
            # catalogue, but never granted.
            if perm not in known and not (
                op.action == "revoke" and (subject, hub, perm) in existing
            ):
                raise HTTPException(422, "Unknown permission for this hub")
            grants = grants or op.action == "grant"
            ops.append((op.action, perm))
        if grants:
            flags = _account_flags(gs, subject)
            if flags["suspended"]:
                raise HTTPException(409, "Reactivate this user before granting access")
            if flags["account_unavailable"]:
                raise HTTPException(409, _UNAVAILABLE_MSG)
        try:
            revision = gs.apply_changes(
                subject, hub, ops,
                expected_revision=payload.expected_revision,
                actor=admin.subject,
            )
        except RevisionConflict as exc:
            raise HTTPException(409, str(exc))
        except (ValueError, LastAdminError) as exc:
            raise HTTPException(409, str(exc))
        return dict(ok=True, revision=revision)

    @router.get("/people")
    def people(
        q: str = "",
        status: str | None = None,
        role: str | None = None,  # admin | regular | service
        agent: str | None = None,  # only people with access to this hub
        offset: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=200),
        admin=Depends(require_admin),
    ):
        gs = store_for(hub_dir)
        scopes = {h["key"] for h in allowed_hubs(admin)}
        if agent:
            require_hub(admin, agent)  # never let a filter widen scope
        grants = [g for g in gs.list_grants() if admin.is_org_admin or g[1] in scopes]
        visible = {g[0] for g in grants}
        rows = []
        for person in gs.identities():
            sub = person["subject"]
            if not admin.is_org_admin and sub not in visible:
                continue
            if q.lower() not in (sub + " " + (person["display"] or "")).lower():
                continue
            person.update(_account_state(gs, sub, person))
            person["organization_admin"] = gs.can(sub, ORG, MANAGE_ACCESS)
            person["access"] = {h: sorted(gs.permissions_for(sub, h)) for h in scopes}
            # Filters, applied before pagination.
            if status and person["status"] != status:
                continue
            if role:
                is_service = sub.startswith("workflow:")
                if role == "admin" and not person["organization_admin"]:
                    continue
                if role == "service" and not is_service:
                    continue
                if role == "regular" and (person["organization_admin"] or is_service):
                    continue
            if agent and not person["access"].get(agent):
                continue
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
        gs = store_for(hub_dir)
        subject = normalize(payload.subject)
        if not subject or subject == "*":
            raise HTTPException(409, "a person or service is required")
        before = _account_flags(gs, subject)
        # Reactivate only clears the admin marker. When it isn't set there is
        # nothing to do: skip the (audit-writing) store call rather than record
        # a "reactivate" that changes nothing.
        changed = payload.suspended or before["suspended"]
        if changed:
            try:
                gs.suspend(subject, actor=admin.subject, suspended=payload.suspended)
            except (LastAdminError, ValueError) as exc:
                raise HTTPException(409, str(exc))
        state = _account_state(gs, subject)
        message = None
        if not payload.suspended and state["account_unavailable"]:
            prefix = "Admin block cleared. " if changed else "Not blocked by an admin. "
            message = prefix + _UNAVAILABLE_MSG
        return dict(ok=True, subject=subject, changed=changed, message=message, **state)

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
        hub: str | None = None,
        workflow: str | None = None,
        run_id: str | None = None,
        status: str | None = None,  # comma list: succeeded|failed|running|cancelled
        since: str | None = None,  # ISO 8601; applied before pagination
        until: str | None = None,
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
        admin=Depends(require_admin),
    ):
        from .workflows.observe import (
            runs as read_runs,
            runs_across,
            resolve_statuses,
        )

        # Validate the status filter up front: an unrecognized value is a client
        # error (422), never silently ignored — dropping it would widen the query to
        # every status instead of narrowing it. An absent filter stays None.
        try:
            statuses = resolve_statuses(status)
        except ValueError:
            raise HTTPException(
                422,
                "Unknown run status filter. Use succeeded, failed, running or cancelled.",
            )

        # A named agent scopes to that one bridge (and carries per-step detail when a
        # run_id is given). No agent → cross-agent history over every agent this
        # administrator may manage; filters and pagination apply to the merged set.
        try:
            if hub:
                path = require_hub(admin, hub)
                rows = read_runs(
                    path,
                    name=workflow,
                    run_id=run_id,
                    statuses=statuses,
                    start=since,
                    end=until,
                    limit=limit,
                    offset=offset,
                )
                return {"runs": rows, "has_more": len(rows) >= limit and not run_id}
            return runs_across(
                allowed_hubs(admin),
                name=workflow,
                run_id=run_id,
                statuses=statuses,
                start=since,
                end=until,
                limit=limit,
                offset=offset,
            )
        except HTTPException:
            raise
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
        outcome: str | None = None,  # allow | deny (supersedes `denied`)
        tool: str | None = None,
        surface: str | None = None,
        since: str | None = None,
        until: str | None = None,
        admin=Depends(require_admin),
    ):
        from .access import audit as auditlib

        decision = outcome if outcome in ("allow", "deny") else ("deny" if denied else None)
        # Filters are applied inside read() before the tail cut, so paging is over
        # the filtered set. `selected()` keeps every hub within the admin's scope.
        rows = [
            dict(r, hub=h["key"])
            for h in selected(admin, hub)
            for r in auditlib.read(
                Path(h["path"]),
                limit=limit + offset,
                user=user,
                decision=decision,
                tool=tool,
                surface=surface,
                since=since,
                until=until,
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
        actor: str | None = None,
        action: str | None = None,
        since: float | None = None,
        until: float | None = None,
        admin=Depends(require_admin),
    ):
        scopes = [h["key"] for h in selected(admin, hub)]
        if admin.is_org_admin and not hub:
            scopes.append(ORG)
        return {
            "rows": store_for(hub_dir).read_access_audit(
                limit, hubs=scopes, subject=user, actor=actor, action=action,
                since=since, until=until, offset=offset,
            )
        }

    @router.post("/sync")
    def sync(request: Request, admin=Depends(require_admin)):
        _check_same_origin(request)
        if not admin.is_org_admin:
            raise HTTPException(403, "Organization admin required")
        from .access.reconcile import sync_owui

        return sync_owui(hub_dir)

    _PERIODS = {"24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}

    @router.get("/summary")
    def summary(period: str = "7d", admin=Depends(require_admin)):
        """The Console home: usage per hub from Hubzoid's own tables (usage,
        access decisions, grants, DBOS runs, missed schedule slots). Nothing is
        read from the chat UI. A number Hubzoid cannot know is null
        ("unavailable"), never zero."""
        import time as _time
        from datetime import datetime, timezone

        from . import db
        from . import usage as usage_lib
        from .access import audit as auditlib
        from .access.identity import normalize

        if period not in _PERIODS:
            raise HTTPException(422, "period must be one of 24h, 7d, 30d")
        now = _time.time()
        since = now - _PERIODS[period]
        hs = allowed_hubs(admin)
        engine = db.operational_engine(hub_dir)
        usage = usage_lib.summary(engine, {Path(h["path"]).name: h["key"] for h in hs}, since)
        denials = auditlib.denials(engine, since, [normalize(Path(h["path"]).name) for h in hs])
        gs = store_for(hub_dir)
        grants = gs.list_grants()
        from .workflows.observe import catalog, run_counts

        with_work = []
        for h in hs:
            try:
                if catalog(Path(h["path"])):
                    with_work.append(h)
            except Exception:  # noqa: BLE001 — a broken workflows/ still has a row
                log.exception("summary: could not read workflows for %s", h["key"])
                with_work.append(h)
        runs, runs_ok = {}, True
        if with_work:
            try:
                runs = run_counts(with_work, datetime.fromtimestamp(since, timezone.utc).isoformat())
            except Exception:  # noqa: BLE001
                log.exception("summary: run history unavailable")
                runs_ok = False
        work_keys = {h["key"] for h in with_work}

        def missed_slots(h) -> int | None:
            """Scheduled slots skipped in the period. Both dispatchers keep dated
            records, [[ISO UTC time, count], ...]: code workflows in the hub's
            runtime health, markdown tasks in each task's schedule state."""
            from .scheduling import ScheduleState, load_tasks

            path = Path(h["path"])
            try:
                logs = [gs.runtime_health(path.name.lower()).get("missed_log") or []]
                state = ScheduleState(path)
                logs += [state.get(t.name).get("missed_log") or [] for t in load_tasks(path)[0]]
            except Exception:  # noqa: BLE001 — unknown, never zero
                log.exception("summary: could not read missed slots for %s", h["key"])
                return None
            total = 0
            for entry in (e for entries in logs for e in entries):
                try:
                    at = datetime.fromisoformat(entry[0])
                    if at.tzinfo is None:
                        at = at.replace(tzinfo=timezone.utc)
                    if at.timestamp() >= since:
                        total += int(entry[1])
                except (TypeError, ValueError, IndexError, KeyError):
                    continue  # a malformed record is skipped
            return total

        rows = []
        for h in hs:
            key = h["key"]
            u = usage["hubs"].get(key, {})
            managed = gs.is_authoritative(key)
            subjects = {g[0] for g in grants if g[1] == key and not g[0].startswith("workflow:")}
            r = runs.get(key, {"runs": 0, "failed": 0, "cancelled": 0}) if runs_ok else None
            rows.append(dict(
                key=key, name=h.get("name") or key, managed=managed,
                chats=u.get("chats", 0), messages=u.get("messages", 0),
                active_users=u.get("active_users", 0),
                input_tokens=u.get("input_tokens", 0), output_tokens=u.get("output_tokens", 0),
                cost_usd=u.get("cost_usd"), unpriced=u.get("unpriced", 0),
                last_activity=u.get("last_activity"),
                # Legacy hubs keep access in the chat app's groups: unknown here.
                users_with_access=len(subjects - {"*"}) if managed else None,
                everyone="*" in subjects if managed else None,
                denials=denials.get(normalize(Path(h["path"]).name), 0),
                has_workflows=key in work_keys,
                runs=r["runs"] if r and key in work_keys else None,
                failed=r["failed"] if r and key in work_keys else None,
                missed=missed_slots(h) if key in work_keys else None,
            ))
        costs = [r["cost_usd"] for r in rows if r["cost_usd"] is not None]
        missed = [r["missed"] for r in rows if r["has_workflows"]]
        totals = dict(
            chats=sum(r["chats"] for r in rows), messages=sum(r["messages"] for r in rows),
            active_users=usage["active_users"],
            input_tokens=sum(r["input_tokens"] for r in rows),
            output_tokens=sum(r["output_tokens"] for r in rows),
            cost_usd=round(sum(costs), 6) if costs else None,
            unpriced=sum(r["unpriced"] for r in rows),
            denials=sum(r["denials"] for r in rows),
            runs=sum(r["runs"] or 0 for r in rows) if with_work and runs_ok else None,
            failed=sum(r["failed"] or 0 for r in rows) if with_work and runs_ok else None,
            missed=sum(missed) if missed and None not in missed else None,
        )
        return dict(period=period, since=since, generated=now,
                    recording_since=usage["recording_since"], has_workflows=bool(with_work),
                    runs_available=runs_ok, totals=totals, hubs=rows)

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
