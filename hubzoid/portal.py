# Hubzoid admin portal. Apache-2.0 licensed like the rest of the repository.
"""The admin portal: a read-mostly JSON API + a static React SPA.

Agents, per-agent access/runs/activity, and deployment people/activity views.
Access and account-policy changes are authorized server-side; workflow views
inspect the state already held by the access store, DBOS and the audit log.
Served by the bridge's FastAPI at `/portal`; the JSON API is under
`/portal/api`.

Auth is the same OWUI/OIDC session as chat, resolved by an injected
`admin_resolver(request) -> PortalAdmin | None`. The production resolver
validates the OWUI session server-side and strips any inbound identity header
(so a browser can't assert its own identity); a dev resolver keys off an env
var. Entry requires organization-wide or per-hub `manage_access`; a chat-app admin
role alone does not grant Console access.

Every endpoint also accepts `Authorization: Bearer sk-...`, an Open WebUI API
key verified server-side against Open WebUI's key table (the same check as the
hosted MCP surface). A key caller has no ambient cookie, so the same-origin
rule applies only to session callers. Any other Authorization value is ignored.

Authority is decided by `access.service.AccessService`, never in a handler:
every mutation builds an `Actor` from the verified identity and calls it.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

import functools

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ConfigDict, SecretStr, ValidationError
from . import deployment
from .access.identity import normalize

from .access import store_for
from .access.service import (
    LEGACY_MSG,
    UNAVAILABLE_MSG,
    AccessService,
    Actor,
    Denied,
)
from .access.session import require_same_origin, verified_email
from .access.store import (
    MANAGE_ACCESS,
    ORG,
    USE_HUB,
    EVERYONE,
)

log = logging.getLogger("hubzoid.portal")


def _truthy_env(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


@dataclass
class PortalAdmin:
    subject: str
    is_org_admin: bool
    manageable: list[str]
    # How the identity was verified: "session" (Open WebUI cookie, or the local
    # dev override) or "api-key" (an Open WebUI API key as a Bearer token).
    via: str = "session"

    def actor(self) -> Actor:
        return Actor(
            subject=normalize(self.subject),
            surface="api" if self.via == "api-key" else "console",
            via=self.via,
        )


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


class AccountGrant(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    hub: str = Field(min_length=1, max_length=200)
    permission: str = Field(min_length=1, max_length=200)


# Passwords are SecretStr so a validation error never echoes them back.
class AccountCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=320)
    name: str = Field(min_length=1, max_length=200)
    password: SecretStr
    grants: list[AccountGrant] = Field(default_factory=list, max_length=200)


class PasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: SecretStr


class RoleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "admin"]


class DeleteAccountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm_email: str = Field(min_length=1, max_length=320)


class ConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    plan_hash: str = Field(min_length=1, max_length=128)
    password: SecretStr | None = None


def _validated(model: type[BaseModel], body: Any) -> Any:
    """Validate a body that may carry a password. FastAPI's own validation
    errors echo the offending input, so these bodies are checked here and the
    error names only the field."""
    try:
        return model.model_validate(body if body is not None else {})
    except ValidationError as exc:
        fields = sorted({".".join(str(x) for x in e["loc"]) or "body" for e in exc.errors()})
        raise Denied(422, "invalid_request",
                     "Check these fields: " + ", ".join(fields) + ".")


def _denied(fn):
    """Turn a service refusal into `{"detail", "code"}` with its status."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Denied as exc:
            return JSONResponse({"detail": exc.message, "code": exc.code},
                                status_code=exc.status)

    return wrapper


def default_admin_resolver(hub_dir: Path) -> Callable[[Request], "PortalAdmin | None"]:
    """Resolve the portal admin from the request.

    Dev: `HUBZOID_PORTAL_DEV_USER=<subject>` trusts that subject (local only).
    Prod: validate the OWUI session cookie via OWUI `GET /api/v1/auths/` and use
    the verified email — never a client-sent identity header. Gated by
    `can(subject, *, manage_access)` (org) or any hub's manage_access.
    """
    hub_dir = Path(hub_dir)

    service = AccessService(hub_dir)

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
            raise HTTPException(401, "Sign in to continue.")
        try:
            scope = service.scope(Actor(normalize(subject), "console", "session"))
        except Denied as exc:
            raise HTTPException(exc.status, exc.message)
        if not scope.any:
            return None
        return PortalAdmin(subject=subject, is_org_admin=scope.org_admin,
                           manageable=sorted(scope.hubs))

    return resolve


def api_key(request: Request) -> str | None:
    """The Open WebUI API key in `Authorization: Bearer sk-...`, else None.

    Only that form selects the API-key path. Any other Authorization value (for
    example HTTP Basic added by a reverse proxy, or a chat-app session token) is
    ignored and the request is treated as a cookie session, as before."""
    scheme, _, token = (request.headers.get("authorization") or "").partition(" ")
    token = token.strip()
    if scheme.lower() == "bearer" and token.startswith("sk-"):
        return token
    return None


def _key_email(request: Request, hub_dir: Path) -> str:
    from .access import owui_api_keys

    email = owui_api_keys.resolve_email(hub_dir, api_key(request))
    if not email:
        raise HTTPException(401, "This API key is not valid.")
    return normalize(email)


def api_key_admin(request: Request, hub_dir: Path) -> "PortalAdmin | None":
    """Resolve `Authorization: Bearer sk-...` to its Open WebUI owner, with the
    same management scope checks as a session."""
    email = _key_email(request, hub_dir)
    try:
        scope = AccessService(hub_dir).scope(Actor(normalize(email), "api", "api-key"))
    except Denied as exc:
        raise HTTPException(exc.status, exc.message)
    if not scope.any:
        return None
    return PortalAdmin(subject=normalize(email), is_org_admin=scope.org_admin,
                       manageable=sorted(scope.hubs), via="api-key")


def _is_loopback(request: Request) -> bool:
    host = getattr(request.client, "host", "") if request.client else ""
    return host in ("127.0.0.1", "::1", "localhost")


_check_same_origin = require_same_origin
_verify_owui_session = verified_email


def _check_mutation(request: Request, admin: PortalAdmin) -> None:
    """Session callers carry an ambient cookie, so their writes must come from
    our own origin. An API-key caller sends its credential explicitly."""
    if admin.via != "api-key":
        _check_same_origin(request)


# ---- account state ----------------------------------------------------------
#
# The store keeps two independent block markers per subject and `is_suspended`
# ORs them: `suspended:<subject>` (an admin's explicit block, or an
# account_replaced safeguard) and `account_unavailable:<subject>` (derived from
# Open WebUI: the account is pending approval, or vanished from the directory).
# Only the first is cleared by "reactivate"; the second only clears when OWUI
# reports the account as approved/present again. The portal exposes them apart
# so the UI can tell "blocked by an admin" from "blocked by the chat app".

_UNAVAILABLE_MSG = UNAVAILABLE_MSG

# A hub whose access is not yet dashboard-managed (Casbin not authoritative) is still
# governed by the chat app. Editing its access here would neither take effect nor
# survive migration, so those edits are refused (in the API, not only the UI).
_LEGACY_MSG = LEGACY_MSG


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
    service = AccessService(hub_dir)

    def require_admin(request: Request) -> PortalAdmin:
        if api_key(request):
            admin = api_key_admin(request, hub_dir)
        else:
            admin = resolver(request)
        if admin is None:
            raise HTTPException(
                403, "Sign in with an account allowed to manage agent access."
            )
        return admin

    def require_person(request: Request) -> Actor:
        """Any verified caller, manager or not: for change requests, whose own
        check is "only the proposer may see or decide it"."""
        if api_key(request):
            return Actor(_key_email(request, hub_dir), "api", "api-key")
        if admin_resolver is not None:
            admin = admin_resolver(request)
            if admin is None:
                raise HTTPException(403, "Sign in to continue.")
            return admin.actor()
        dev = (os.environ.get("HUBZOID_PORTAL_DEV_USER") or "").strip()
        subject = dev if dev and _truthy_env("HUBZOID_PORTAL_DEV") else ""
        subject = subject or _verify_owui_session(request, hub_dir)
        if not subject:
            raise HTTPException(401, "Sign in to continue.")
        return Actor(normalize(subject), "console", "session")

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

    def _grantable(admin, hub) -> list[str]:
        try:
            return sorted(service.ceiling(admin.actor(), hub))
        except Denied:
            return []

    def selected(admin, hub):
        if hub:
            require_hub(admin, hub)
            return [h for h in allowed_hubs(admin) if h["key"] == hub]
        return allowed_hubs(admin)

    @router.get("/chat-access")
    def chat_access(request: Request):
        """Used by the edge to filter OWUI's picker, including OWUI admins.

        This is not the Console admin gate. Ordinary signed-in members need
        their own effective entry decision too. The bridge still enforces entry.
        """
        # Scripts and chat clients call the chat app's API with a bearer token
        # and no cookie. They got the model list before the edge filtered it.
        subject = _verify_owui_session(request, hub_dir, bearer=True)
        if not subject:
            raise HTTPException(401, "Sign in to see your agents.")
        gs = store_for(hub_dir)
        blocked = gs.is_suspended(subject)
        denied = []
        for h in deployment.hubs(hub_dir):
            if blocked or (gs.is_authoritative(h["key"]) and not gs.can(subject, h["key"], USE_HUB)):
                denied.append(h["model_id"])
        return {"denied": denied}

    @router.get("/me")
    @_denied
    def me(brief: bool = False, admin=Depends(require_admin)):
        out = dict(
            subject=admin.subject,
            org_admin=admin.is_org_admin,
            manageable=[h["key"] for h in allowed_hubs(admin)],
        )
        if brief:  # the chat sidebar link only needs to know the Console opens
            return out
        from .access import accounts as accountlib

        actor = admin.actor()
        out.update(
            grantable=service.grantable(actor),
            account_admin=admin.is_org_admin,
            can_create_accounts=service.can_create_accounts(actor),
            accounts_configured=accountlib.configured(hub_dir),
            via=admin.via,
        )
        return out

    @router.get("/hubs")
    def hubs(admin=Depends(require_admin)):
        gs = store_for(hub_dir)
        return {
            "hubs": [
                dict(
                    key=h["key"],
                    name=h["name"],
                    model_id=h["model_id"],
                    can_chat=not gs.is_suspended(admin.subject) and (not gs.is_authoritative(h["key"]) or gs.can(admin.subject, h["key"], USE_HUB)),
                    authoritative=gs.is_authoritative(h["key"]),
                )
                for h in allowed_hubs(admin)
            ]
        }

    @router.get("/permissions")
    @_denied
    def permissions(hub: str, admin=Depends(require_admin)):
        require_hub(admin, hub)
        return dict(hub=hub, permissions=service.catalog(hub))

    @router.get("/access")
    @_denied
    def access(
        hub: str,
        q: str = "",
        offset: int = Query(0, ge=0),
        limit: int = Query(50, ge=1, le=200),
        admin=Depends(require_admin),
    ):
        require_hub(admin, hub)
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
        # Chat accounts that enter only through "everyone signed in": signed
        # up, approved, not blocked, and without a direct grant in this hub.
        # Shown before an administrator removes that grant.
        public_reliant = 0
        if public:
            direct = {s for (s, h, p) in all_grants if h == hub and p == USE_HUB}
            for ident in gs.identities():
                subject = ident["subject"]
                if (ident.get("owui_id") and not ident.get("pending") and subject != EVERYONE
                        and not subject.startswith("workflow:") and subject not in direct
                        and not gs.is_suspended(subject)):
                    public_reliant += 1
        return dict(
            hub=hub,
            # Editable only once the hub is dashboard-managed. A legacy (un-migrated)
            # hub is read-only here; its access still lives in the chat app.
            editable=gs.is_authoritative(hub),
            authoritative=gs.is_authoritative(hub),
            can_manage_admins=admin.is_org_admin,
            permissions=service.catalog(hub),
            # What this viewer may grant or remove here (a delegate's ceiling).
            # Display only: every write is checked again by the service.
            grantable=_grantable(admin, hub) if gs.is_authoritative(hub) else [],
            viewer=normalize(admin.subject),
            total=len(result),
            public=public,
            public_reliant=public_reliant,
            revision=revision,
            rows=result[offset : offset + limit],
        )

    def mutate(request, payload, admin, revoke=False):
        _check_mutation(request, admin)
        revision = service.apply_access_change(
            admin.actor(), payload.subject, payload.hub,
            [("revoke" if revoke else "grant", payload.permission)],
            expected_revision=payload.expected_revision,
        )
        return dict(ok=True, revision=revision)

    @router.post("/access/grant")
    @_denied
    def grant(request: Request, payload: GrantRequest, admin=Depends(require_admin)):
        return mutate(request, payload, admin)

    @router.post("/access/revoke")
    @_denied
    def revoke(request: Request, payload: GrantRequest, admin=Depends(require_admin)):
        return mutate(request, payload, admin, True)

    @router.post("/access/apply")
    @_denied
    def apply(request: Request, payload: ApplyRequest, admin=Depends(require_admin)):
        """Apply one person's whole change set for a hub in a single guarded
        transaction. Atomic: either every operation applies on the expected
        revision, or none does (409 on a concurrent change)."""
        _check_mutation(request, admin)
        subject = normalize(payload.subject)
        hub = normalize(payload.hub)
        # Request shape for this endpoint: organization roles and public access
        # have their own controls. Authority is decided by the service.
        if hub == ORG:
            raise HTTPException(400, "Organization admin rights are changed per person, not here")
        if subject == EVERYONE:
            raise HTTPException(
                403, "Access for everyone signed in can't be granted. An organization "
                "administrator can remove an existing one from the access list.")
        revision = service.apply_access_change(
            admin.actor(), subject, hub,
            [(op.action, op.permission) for op in payload.operations],
            expected_revision=payload.expected_revision,
        )
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
    @_denied
    def refresh_people(request: Request, admin=Depends(require_admin)):
        _check_mutation(request, admin)
        return {"ok": True, "count": service.refresh_accounts(admin.actor())}

    @router.post("/people/block")
    @_denied
    def block(request: Request, payload: PersonRequest, admin=Depends(require_admin)):
        _check_mutation(request, admin)
        gs = store_for(hub_dir)
        subject = normalize(payload.subject)
        # Reactivate only clears the admin marker. When it isn't set there is
        # nothing to do: the service skips the (audit-writing) store call rather
        # than record a "reactivate" that changes nothing.
        changed = service.set_blocked(admin.actor(), subject, payload.suspended)
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
    @_denied
    def sync(request: Request, admin=Depends(require_admin)):
        _check_mutation(request, admin)
        return service.sync_visibility(admin.actor())

    # ---- accounts (Open WebUI logins, created and changed as the service account)

    @router.post("/accounts")
    @_denied
    def create_account(request: Request, body: Any = Body(None), admin=Depends(require_admin)):
        _check_mutation(request, admin)
        payload = _validated(AccountCreate, body)
        created = service.create_account(
            admin.actor(), email=payload.email, name=payload.name,
            password=payload.password.get_secret_value(),
            grants=[(g.hub, g.permission) for g in payload.grants],
        )
        return dict(ok=True, **created)

    @router.get("/accounts/{subject}")
    @_denied
    def account_info(subject: str, admin=Depends(require_admin)):
        return service.account_info(admin.actor(), subject)

    @router.post("/accounts/{subject}/password")
    @_denied
    def reset_password(subject: str, request: Request, body: Any = Body(None),
                       admin=Depends(require_admin)):
        _check_mutation(request, admin)
        payload = _validated(PasswordRequest, body)
        service.set_password(admin.actor(), subject, payload.password.get_secret_value())
        return dict(ok=True, subject=normalize(subject))

    @router.post("/accounts/{subject}/approve")
    @_denied
    def approve_account(subject: str, request: Request, admin=Depends(require_admin)):
        _check_mutation(request, admin)
        service.approve_account(admin.actor(), subject)
        return dict(ok=True, subject=normalize(subject))

    @router.post("/accounts/{subject}/role")
    @_denied
    def chat_role(subject: str, request: Request, payload: RoleRequest,
                  admin=Depends(require_admin)):
        _check_mutation(request, admin)
        service.set_chat_role(admin.actor(), subject, payload.role)
        return dict(ok=True, subject=normalize(subject), role=payload.role)

    @router.delete("/accounts/{subject}")
    @_denied
    def delete_account(subject: str, request: Request,
                       payload: DeleteAccountRequest = Body(...),
                       admin=Depends(require_admin)):
        _check_mutation(request, admin)
        if normalize(payload.confirm_email) != normalize(subject):
            raise Denied(422, "confirm_email", "Type the account's email to confirm.")
        service.delete_account(admin.actor(), subject)
        return dict(ok=True, subject=normalize(subject))

    # ---- change requests (proposed by agent tools, confirmed here) -------------

    @router.get("/change-requests/{request_id}")
    @_denied
    def get_change_request(request_id: str, actor=Depends(require_person)):
        return service.get_request(actor, request_id)

    @router.post("/change-requests/{request_id}/confirm")
    @_denied
    def confirm_change_request(request_id: str, request: Request, body: Any = Body(None),
                               actor=Depends(require_person)):
        if actor.via != "api-key":
            _check_same_origin(request)
        payload = _validated(ConfirmRequest, body)
        return service.confirm(
            actor, request_id, plan_hash=payload.plan_hash,
            password=payload.password.get_secret_value() if payload.password else None,
        )

    @router.post("/change-requests/{request_id}/reject")
    @_denied
    def reject_change_request(request_id: str, request: Request,
                              actor=Depends(require_person)):
        if actor.via != "api-key":
            _check_same_origin(request)
        service.reject(actor, request_id)
        return dict(ok=True, id=request_id, status="rejected")

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
    from . import connect_journey
    app.include_router(connect_journey.build_router(hub_dir))
    dist = Path(__file__).parent / "portal_dist"
    if dist.is_dir():
        from fastapi.staticfiles import StaticFiles

        app.mount("/portal", StaticFiles(directory=str(dist), html=True), name="portal")
        log.info("portal: serving SPA from %s", dist)
    else:
        log.info("portal: API mounted; static SPA not built (no portal_dist/)")
