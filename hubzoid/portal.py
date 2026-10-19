# Hubzoid admin portal. Apache-2.0 licensed like the rest of the repository.
"""The admin portal: a read-mostly JSON API + a static React SPA.

Agents, per-agent access/runs/activity, and deployment people/activity views.
Access and account-policy changes are authorized server-side; workflow views
inspect the state already held by the access store, DBOS and the audit log.
Served by the bridge's FastAPI at `/portal`; the JSON API is under
`/portal/api`.

Auth is the same Hubzoid session as chat, resolved by an injected
`admin_resolver(request) -> PortalAdmin | None`. The production resolver
validates the session server-side and never reads a client-sent identity
header; a dev resolver keys off an env var. Entry requires organization-wide
or per-hub `manage_access`; a chat-app admin role alone does not grant
Console access. The web app's Settings page shows these screens.

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
import shlex
from urllib.parse import quote

from fastapi import APIRouter, Body, Depends, HTTPException, Request, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ConfigDict, SecretStr, ValidationError
from . import deployment
from .access.identity import normalize

from .access import store_for
from .access.service import (  # noqa: F401 — the account-state helpers stay importable here
    UNAVAILABLE_MSG,
    AccessService,
    Actor,
    Denied,
    Scope,
    _account_flags,
    _account_state,
    _account_status,
    deleted_by_console,
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
    # How the identity was verified: "session" (the Hubzoid session, or the
    # local dev override).
    via: str = "session"

    def actor(self) -> Actor:
        return Actor(
            subject=normalize(self.subject),
            surface="console",
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


class HubAction(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    hub: str = Field(min_length=1, max_length=255)


class ScheduleEdit(HubAction):
    inputs: dict = Field(default_factory=dict)
    cadence: str = Field(min_length=1, max_length=200)
    timezone: str = Field(min_length=1, max_length=100)
    revision: float


class CompanyLinkAction(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    hub: str = Field(min_length=1, max_length=255)
    action: Literal["copy", "replace", "off"]


class WorkflowRun(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    hub: str = Field(min_length=1, max_length=255)
    workflow: str = Field(min_length=1, max_length=255)
    inputs: dict = Field(default_factory=dict)


class AccountGrant(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    hub: str = Field(min_length=1, max_length=200)
    permission: str = Field(min_length=1, max_length=200)


# Passwords are SecretStr so a validation error never echoes them back.
class AccountCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=320)
    name: str = Field(min_length=1, max_length=200)
    # "password" (the default) needs `password`; "google" (Google sign-in only)
    # takes none: the server sets a random one nobody is told.
    sign_in: Literal["password", "google"] = "password"
    password: SecretStr | None = None
    grants: list[AccountGrant] = Field(default_factory=list, max_length=200)
    # For WhatsApp and Telegram (optional): the number their messages come from.
    phone: str | None = Field(default=None, max_length=32)


class ExistingAccountGrant(BaseModel):
    """Access for an account that already exists (never creates one)."""
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    email: str = Field(min_length=3, max_length=320)
    grants: list[AccountGrant] = Field(min_length=1, max_length=200)


class PasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: SecretStr


class LinkPasswordRequest(BaseModel):
    """Default mode: no password makes a one-time link to set one."""
    model_config = ConfigDict(extra="forbid")
    password: SecretStr | None = None


class RoleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "admin"]


class PhoneRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")
    # Empty clears it. Digits, spaces, dashes, brackets and a leading +.
    phone: str = Field(default="", max_length=32)


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
            # `extra` is structured detail the service marks safe to show
            # (e.g. which account a partial create made); never a secret.
            return JSONResponse({**exc.extra, "detail": exc.message, "code": exc.code},
                                status_code=exc.status)

    return wrapper


def default_admin_resolver(hub_dir: Path) -> Callable[[Request], "PortalAdmin | None"]:
    """Resolve the portal admin from the request.

    Dev: `HUBZOID_PORTAL_DEV_USER=<subject>` trusts that subject (local only).
    Prod: the verified email of the Hubzoid session — never a client-sent
    identity header. Gated by
    `can(subject, *, manage_access)` (org) or any hub's manage_access.
    """
    hub_dir = Path(hub_dir)

    service = AccessService(hub_dir)

    def resolve(request: Request) -> "PortalAdmin | None":
        # The dev override is a deliberate TWO-part opt-in: both
        # HUBZOID_PORTAL_DEV=1 AND HUBZOID_PORTAL_DEV_USER must be set. It bypasses
        # the session check, so it is strictly for `hubzoid run` local development
        # and MUST NOT be set on any deployment with a public edge (the edge
        # proxies over loopback, so a client-IP check can't distinguish local from
        # remote). Everywhere else, identity comes only from a verified session.
        subject = ""
        dev = (os.environ.get("HUBZOID_PORTAL_DEV_USER") or "").strip()
        if dev and _truthy_env("HUBZOID_PORTAL_DEV"):
            log.warning(
                "portal: HUBZOID_PORTAL_DEV is ON — trusting dev user %r without a "
                "session. NEVER set this on a public deployment.",
                dev,
            )
            subject = dev
        if not subject:
            subject = _verify_session(request, hub_dir)
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


def _is_loopback(request: Request) -> bool:
    host = getattr(request.client, "host", "") if request.client else ""
    return host in ("127.0.0.1", "::1", "localhost")


_check_same_origin = require_same_origin
_verify_session = verified_email


def _check_mutation(request: Request, admin: PortalAdmin) -> None:
    """Callers carry an ambient session cookie, so their writes must come from
    our own origin."""
    _check_same_origin(request)


# ---- account state ----------------------------------------------------------
#
# The two block markers and the display status live in `access.service`
# (`_account_state`), shared with the agent tools. The portal exposes the
# markers apart so the UI can tell "blocked by an admin" from "blocked by the
# chat app".

_UNAVAILABLE_MSG = UNAVAILABLE_MSG

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
    unavailable account that is *not* pending (deleted/missing) is
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


def _subject_kind(subject: str) -> str:
    if subject.startswith("workflow:"):
        return "service"
    return "person"


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
        admin = resolver(request)
        if admin is None:
            raise HTTPException(
                403, "Sign in with an account allowed to manage agent access."
            )
        return admin

    def require_person(request: Request) -> Actor:
        """Any verified caller, manager or not: for change requests, whose own
        check is "only the proposer may see or decide it"."""
        if admin_resolver is not None:
            admin = admin_resolver(request)
            if admin is None:
                raise HTTPException(403, "Sign in to continue.")
            return admin.actor()
        dev = (os.environ.get("HUBZOID_PORTAL_DEV_USER") or "").strip()
        subject = dev if dev and _truthy_env("HUBZOID_PORTAL_DEV") else ""
        subject = subject or _verify_session(request, hub_dir)
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
        # The web app owns sign-in and personal connections (always, since 2.0;
        # kept for clients that still read it).
        out["web_app"] = True
        from .access import accounts as accountlib

        actor = admin.actor()
        out.update(
            grantable=service.grantable(actor),
            account_admin=admin.is_org_admin,
            can_create_accounts=service.can_create_accounts(actor),
            accounts_configured=True,
            # How a new account can sign in: {"password", "google", "google_domains"?}.
            sign_in=accountlib.sign_in_options(hub_dir),
            via=admin.via,
        )
        return out

    @router.get("/hubs")
    def hubs(admin=Depends(require_admin)):
        from .loaders import agents as agents_loader

        gs = store_for(hub_dir)
        return {
            "hubs": [
                dict(
                    key=h["key"],
                    name=agents_loader.display_name(Path(h["path"]), h["name"]),
                    path=str(Path(h["path"]).resolve()),
                    model_id=h["model_id"],
                    can_chat=not gs.is_suspended(admin.subject) and gs.can(admin.subject, h["key"], USE_HUB),
                )
                for h in allowed_hubs(admin)
            ]
        }

    @router.get("/hub-mcp-servers")
    @_denied
    def hub_mcp_servers(hub: str, admin=Depends(require_admin)):
        """The hub folder's MCP servers (connectors/.mcp.json), read-only: names,
        kind and which environment variables they need. Never a value."""
        from .loaders import mcp as mcp_loader

        return mcp_loader.describe(require_hub(admin, hub))

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
        # Who has access, from the service; this route adds only what depends
        # on the viewer (their scope, ceiling and filter) and pagination.
        view = service.hub_access(admin.actor(), hub,
                                  scope=Scope(admin.is_org_admin, tuple(admin.manageable)))
        result = [
            r
            for r in view["rows"]
            if q.lower() in (r["subject"] + " " + r["display"]).lower()
        ]
        return dict(
            hub=hub,
            can_manage_admins=admin.is_org_admin,
            permissions=service.catalog(hub),
            # What this viewer may grant or remove here (a delegate's ceiling).
            # Display only: every write is checked again by the service.
            grantable=_grantable(admin, hub),
            viewer=normalize(admin.subject),
            total=len(result),
            public=view["public"],
            public_reliant=view["public_reliant"],
            revision=view["revision"],
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
        all_grants = gs.list_grants()
        grants = [g for g in all_grants if admin.is_org_admin or g[1] in scopes]
        visible = {g[0] for g in grants}
        holding = {g[0] for g in all_grants}
        rows = []
        for person in gs.identities():
            sub = person["subject"]
            if not admin.is_org_admin and sub not in visible:
                continue
            # A user deleted here is gone: listed again only if access is
            # given to their email later, or a new account takes it.
            if sub not in holding and deleted_by_console(gs, sub, person.get("owui_id")):
                continue
            if q.lower() not in (sub + " " + (person["display"] or "")).lower():
                continue
            person.update(_account_state(gs, sub, person))
            if not admin.is_org_admin:
                person.pop("phone", None)  # personal data: organization administrators
            person["organization_admin"] = gs.can(sub, ORG, MANAGE_ACCESS)
            person["reader"] = gs.is_reader(sub)
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
        from .workflows.runtime import input_schemas

        out = []
        for h in selected(admin, hub):
            schemas = input_schemas(Path(h["path"]), h["key"])
            for w in catalog(Path(h["path"])):
                w["input_schema"] = schemas.get(w["name"])
                out.append(w)
        return {"workflows": out}

    @router.post("/workflows/run")
    @_denied
    def run_workflow(request: Request, payload: WorkflowRun, admin=Depends(require_admin)):
        """Run now, from the Console: a team run with inputs, recorded as
        started by this admin. Needs Run and control workflows (or org admin)."""
        from .workflows import client, control
        from .workflows import inputs as inputs_lib

        _check_mutation(request, admin)
        path = Path(require_hub(admin, payload.hub))
        gs = store_for(hub_dir)
        from .workflows import access as wf_access

        try:
            key = wf_access.canonical(path, payload.workflow)
        except wf_access.AccessError:
            key = payload.workflow
        if not wf_access.authorize(path, admin.subject, key, "run").allowed:
            raise HTTPException(403, f"You can't run {payload.workflow}. Give yourself run access in "
                                     "Settings, Workflow access.")
        if control._engine_here(path):
            try:
                out = control.start_now(path, payload.workflow, actor=admin.subject, surface="console",
                                        prefer="code", inputs=payload.inputs or None)
            except control.ControlError as exc:
                raise HTTPException(409, exc.message)
            return {"run_id": out["run_id"], "already_running": out["already_running"]}
        from .workflows import observe
        from .workflows.runtime import input_schemas

        # The engine runs in another process: the same hold and one-run-at-a-time
        # rules as a start on the engine's own process.
        if gs.schedule_hold():
            raise HTTPException(409, "New runs are on hold while a backup runs. Try again in a few minutes.")
        name = payload.workflow.replace("-", "_")
        active = observe.runs(path, name=name, statuses=["PENDING", "ENQUEUED", "DELAYED"], limit=1)
        if active:
            return {"run_id": active[0]["id"], "already_running": True}
        shown = inputs_lib.display_with_schema(
            input_schemas(path, normalize(payload.hub)).get(name), payload.inputs or None)
        envelope = {"trigger": "operator", "scope": "team", "started_by": admin.subject,
                    "inputs": payload.inputs or None, "inputs_display": shown}
        live = client.enqueue_live(path, name, envelope=envelope)
        if live is None:
            raise HTTPException(409, "The workflow engine for this agent isn't running.")
        run_id = live.get_workflow_id()
        live.client.destroy()
        return {"run_id": run_id, "already_running": False}

    @router.get("/schedules")
    @_denied
    def schedules(hub: str, admin=Depends(require_admin)):
        """People's schedules on one agent: who, what, when and how it went.
        Never the report itself: that stays with its owner."""
        import time

        from .usage import run_cost
        from .workflows import personal
        from .workflows.runtime import input_schemas

        path = Path(require_hub(admin, hub))
        key = normalize(hub)
        schemas = input_schemas(path, key)
        since = time.time() - 30 * 86400
        rows = [dict(personal.row(s, schemas.get(s.workflow)), owner=s.owner,
                     cost_30d=round(run_cost(path, key, f"ps-{s.id}-", since), 4))
                for s in personal.for_hub(path, key)]
        return {"schedules": rows}

    @router.get("/schedules/{schedule_id}/runs")
    @_denied
    def schedule_runs(schedule_id: str, hub: str, admin=Depends(require_admin)):
        """A person's schedule's latest runs, for admins: status and times only."""
        from .workflows import observe, personal

        path = Path(require_hub(admin, hub))
        found = personal.get(path, schedule_id)
        if found is None or found.hub != normalize(hub):
            raise HTTPException(404, "No such schedule on this agent.")
        return {"runs": observe.schedule_runs(path, found.id)}

    # ---- a person's own schedules and reports (any signed-in account) --------

    def require_viewer(request: Request) -> tuple[str, str]:
        """(account id, email) of the signed-in person, from a live session
        check in either UI mode. Ownership needs both: a new account that
        reuses an email is someone else."""
        from .access import session as session_lib

        who = session_lib.verified_person(request, hub_dir)
        if not who:
            raise HTTPException(401, "Sign in to continue.")
        return str(who[0]), normalize(who[1])

    def person_hub(hub: str) -> tuple[Path, str]:
        key = normalize(hub)
        for h in deployment.hubs(hub_dir):
            if normalize(h["key"]) == key:
                return Path(h["path"]), key
        raise HTTPException(404, "No such agent here.")

    def own_schedule(path: Path, key: str, schedule_id: str, email: str, account: str):
        from .workflows import personal

        found = personal.get(path, schedule_id)
        if (found is None or found.hub != key or found.state == "stopped"
                or not personal.owned_by(path, found, email, account)):
            raise HTTPException(404, "This schedule isn't yours, or it no longer exists.")
        return found

    @router.get("/my")
    @_denied
    def my(request: Request):
        """The signed-in person's own schedules and report links, on every agent.
        `api` is where each agent takes actions (its own bridge in a gateway)."""
        from .artifacts import series as series_lib
        from .chat.common import api_bases
        from .workflows import personal
        from .workflows.runtime import input_schemas, title_for, tool_permission

        account, email = require_viewer(request)
        try:
            is_admin = resolver(request) is not None
        except HTTPException:
            is_admin = False
        from .access.store import USE_HUB

        gs = store_for(hub_dir)
        blocked = gs.is_suspended(email)
        bases = api_bases(hub_dir)
        agents, schedules, reports, catalog = [], [], [], []
        for h in deployment.hubs(hub_dir):
            path, key = Path(h["path"]), normalize(h["key"])
            mine = [s for s in personal.for_owner(path, key, email)
                    if personal.owned_by(path, s, email, account)]
            made = [s for s in series_lib.for_owner(path, key, email) if series_lib.owned(path, s, email, account)]
            usable = not blocked and gs.can(email, key, USE_HUB)
            if not (mine or made or usable):
                continue
            model = h.get("model_id") or key
            chat = f"/?agent={quote(model)}" if usable else None
            agents.append(dict(key=key, name=h.get("name") or key, api=bases.get(key, "") + "/portal/api",
                               chat=chat))
            schemas = input_schemas(path, key) if mine or usable else {}
            if usable:
                from .workflows.observe import definitions
                from .capabilities import switched_off
                from .tools.report_tools import SCHEDULE_REPORTS, RESERVED_NAMES
                from .workflows.boot import schedules_enabled
                from .workflows import access as wf_access

                if schedules_enabled() and not switched_off(SCHEDULE_REPORTS):
                    for wf in definitions(path):
                        if (not wf.get("as_tool") or wf.get("error") or wf["name"].lower() in RESERVED_NAMES
                                or (wf.get("surfaces") and "web" not in wf["surfaces"])
                                or not wf_access.authorize(path, email, wf["name"], "run").allowed):
                            continue
                        catalog.append(dict(hub=key, workflow=wf["name"], title=wf["title"],
                            schedulable=bool(wf.get("schedulable")
                                             and wf_access.authorize(path, email, wf["name"], "schedule").allowed),
                            schema=schemas.get(wf["name"])))
            for s in mine:
                links = [dict(title=x.title or x.key, url=series_lib.latest_path(x))
                         for x in made if x.id in s.series or x.schedule_id == s.id]
                schedules.append(dict(personal.row(s, schemas.get(s.workflow)), hub=key, reports=links))
            for x in made:
                st = series_lib.state(path, x)
                reports.append(dict(id=x.id, hub=key, title=x.title or x.key, report=title_for(x.workflow),
                                    url=series_lib.latest_path(x), updated=x.updated_at, state=st["state"],
                                    company_on=bool(series_lib.domain_token(path, x))))
        return {"subject": email, "admin": is_admin, "blocked": blocked, "web_app": True, "agents": agents,
                "schedules": schedules, "reports": reports, "catalog": catalog,
                "connections": _has_connections(hub_dir, account, email)}

    @router.get("/my/runs")
    @_denied
    def my_runs(hub: str, request: Request, run_id: str | None = None):
        from .workflows import observe
        from .access.store import USE_HUB

        account, email = require_viewer(request)
        path, key = person_hub(hub)
        gs = store_for(path)
        if gs.is_suspended(email) or not gs.can(email, key, USE_HUB):
            raise HTTPException(403, "You no longer have access to this agent.")
        return {"runs": observe.personal_runs(path, email, account, run_id=run_id)}

    @router.get("/my/schedules/{schedule_id}")
    @_denied
    def my_schedule_edit(schedule_id: str, hub: str, request: Request):
        from .workflows.runtime import input_schemas

        account, email = require_viewer(request)
        path, key = person_hub(hub)
        s = own_schedule(path, key, schedule_id, email, account)
        schema = input_schemas(path, key).get(s.workflow)
        if schema is None:
            from .workflows.observe import definitions

            declaration = next((d for d in definitions(path) if d["name"] == s.workflow), None)
            if declaration and not declaration.get("has_inputs") and not declaration.get("error"):
                schema = {"type": "object", "properties": {}}
        return dict(inputs=s.values, cadence=s.cadence, timezone=s.timezone, revision=s.updated, schema=schema)

    @router.put("/my/schedules/{schedule_id}")
    @_denied
    def save_my_schedule(schedule_id: str, request: Request, payload: ScheduleEdit):
        from .workflows import personal

        _check_same_origin(request)
        account, email = require_viewer(request)
        path, key = person_hub(payload.hub)
        s = own_schedule(path, key, schedule_id, email, account)
        try:
            personal.edit(path, key, email, s.id, inputs=payload.inputs, cadence=payload.cadence,
                          timezone_name=payload.timezone, revision=payload.revision)
        except personal.ScheduleError as exc:
            raise HTTPException(409, str(exc))
        store_for(hub_dir).audit_event(email, "schedule_edit", hub=key, permission=s.id, surface="console")
        return {"ok": True}

    @router.get("/my/schedules/{schedule_id}/runs")
    @_denied
    def my_schedule_runs(schedule_id: str, hub: str, request: Request):
        from .workflows import observe

        account, email = require_viewer(request)
        path, key = person_hub(hub)
        found = own_schedule(path, key, schedule_id, email, account)
        return {"runs": observe.schedule_runs(path, found.id)}

    @router.post("/my/schedules/{schedule_id}/{action}")
    @_denied
    def my_schedule_action(schedule_id: str, action: Literal["pause", "resume", "run_now", "stop"],
                           request: Request, payload: HubAction):
        """Pause, resume, run now or stop one's own schedule. Run now starts on
        the agent's own engine, so the page sends it to that agent's bridge."""
        from .workflows import control, personal

        _check_same_origin(request)
        account, email = require_viewer(request)
        path, key = person_hub(payload.hub)
        found = own_schedule(path, key, schedule_id, email, account)
        try:
            if action == "run_now":
                if not control._engine_here(path):
                    raise HTTPException(409, "Reports can't run on this agent right now. Try again later.")
                personal.run_now(path, key, email, found.id)
            elif action == "stop":
                personal.owner_stop(path, key, email, found.id)
            else:
                personal.set_paused(path, key, email, found.id, action == "pause")
        except personal.ScheduleError as exc:
            raise HTTPException(409, str(exc))
        store_for(hub_dir).audit_event(email, f"schedule_{action}", hub=key, permission=found.id,
                                       surface="console")
        return {"ok": True}

    @router.post("/schedules/{schedule_id}/{action}")
    @_denied
    def schedule_action(schedule_id: str, action: Literal["pause", "resume", "stop"],
                        request: Request, payload: HubAction, admin=Depends(require_admin)):
        """Admins pause, resume what they paused, or stop. Never edit inputs."""
        from .workflows import personal

        _check_mutation(request, admin)
        path = Path(require_hub(admin, payload.hub))
        key = normalize(payload.hub)
        try:
            if action == "stop":
                personal.admin_stop(path, key, admin.subject, schedule_id)
            else:
                personal.set_paused(path, key, admin.subject, schedule_id, action == "pause", admin=True)
        except personal.ScheduleError as exc:
            raise HTTPException(409, str(exc))
        store_for(hub_dir).audit_event(admin.subject, f"schedule_{action}", hub=key,
                                       permission=schedule_id, surface="console")
        return {"ok": True}

    @router.get("/reports")
    @_denied
    def reports(hub: str, admin=Depends(require_admin)):
        """One row per live report link on an agent, with its company link."""
        from .artifacts import domains
        from .artifacts import series as series_lib
        from .workflows.runtime import title_for

        path = Path(require_hub(admin, hub))
        on, why = domains.status_for(path, normalize(hub))
        rows = []
        for s in series_lib.for_hub(path, normalize(hub)):
            state = series_lib.state(path, s)
            token = series_lib.domain_token(path, s)
            rows.append(dict(
                id=s.id, title=s.title or s.key, key=s.key, workflow=s.workflow,
                report=title_for(s.workflow), scope=s.scope, owner=s.owner or None,
                updated=s.updated_at, state=state["state"], next=state["next"],
                audience=s.audience, company_on=bool(token)))
        return {"reports": rows, "company": {"on": on, "reason": why, "domains": domains.label()}}

    @router.post("/reports/{series_id}/company")
    @_denied
    def report_company(series_id: str, request: Request, payload: CompanyLinkAction,
                       admin=Depends(require_admin)):
        """Copy, replace or turn off a report's company link. Admins can turn off
        any link, a personal one included (its owner is told); copying and
        replacing are for team reports."""
        from .artifacts import domains
        from .artifacts import series as series_lib

        _check_mutation(request, admin)
        path = Path(require_hub(admin, payload.hub))
        found = series_lib.get(path, series_id)
        if found is None or found.hub != normalize(payload.hub):
            raise HTTPException(404, "No such report on this agent.")
        if payload.action == "off":
            series_lib.domain_off(path, found, admin.subject)
            if not found.team:
                _tell_owner_link_off(path, found)
            result = {"ok": True, "on": False}
        elif not found.team:
            raise HTTPException(403, "Only the owner shares a personal report. Admins can turn its link off.")
        elif payload.action == "copy":
            token = series_lib.domain_token(path, found)
            if not token:
                raise HTTPException(409, "This report has no company link. The workflow turns it on.")
            result = {"ok": True, "on": True, "url": series_lib.share_url(path, token)}
        else:
            on, why = domains.status_for(path, found.hub)
            if not on:
                raise HTTPException(409, why)
            token = series_lib.domain_replace(path, found, admin.subject)
            result = {"ok": True, "on": True, "url": series_lib.share_url(path, token)}
        store_for(hub_dir).audit_event(admin.subject, f"report_link_{payload.action}",
                                       hub=normalize(payload.hub), permission=series_id, surface="console")
        return result

    @router.get("/webhooks")
    @_denied
    def webhooks(hub: str, admin=Depends(require_admin)):
        """A hub's webhook workflows: address, verification, the last 24 hours'
        deliveries by state and the latest failures. Read-only. Never a payload,
        a digest or a header."""
        import time

        from sqlalchemy import text

        from .inbound.routing import hub_slug
        from .artifacts import public_base_url
        from .workflows import events
        from .workflows.observe import catalog

        path = Path(require_hub(admin, hub))
        try:
            specs = events.declarations(path)
        except ValueError as exc:
            return {"webhooks": [], "error": str(exc)}
        if not specs:
            return {"webhooks": []}
        by_webhook: dict[str, list[str]] = {}
        for w in catalog(path):
            if w.get("webhook"):
                by_webhook.setdefault(w["webhook"], []).append(w["name"])
        base = public_base_url(path)
        slug = hub_slug(path, {})
        since = time.time() - 86400
        out = []
        with events._engine(path).connect() as conn:  # noqa: SLF001 — read model
            for name, spec in sorted(specs.items()):
                counts = {s: 0 for s in ("accepted", "running", "succeeded", "failed")}
                for state, n in conn.execute(text(
                        "SELECT state, count(*) FROM hz_workflow_events WHERE hub=:h AND "
                        "webhook=:w AND created >= :t GROUP BY state"),
                        {"h": normalize(hub), "w": name, "t": since}):
                    counts["running" if state == "retrying" else state] = (
                        counts.get("running" if state == "retrying" else state, 0) + int(n))
                failures = [dict(r) for r in conn.execute(text(
                    "SELECT id, workflow, created, updated, attempt, error FROM "
                    "hz_workflow_events WHERE hub=:h AND webhook=:w AND state='failed' "
                    "ORDER BY updated DESC LIMIT 5"), {"h": normalize(hub), "w": name}).mappings()]
                for f in failures:
                    f["redrive"] = f"hubzoid schedule redrive {shlex.quote(f['id'])} --hub {shlex.quote(str(path.resolve()))}"
                out.append(dict(
                    name=name, url=f"{base}/webhooks/{slug}/{name}",
                    verify=spec.get("verify", "header"), workflows=by_webhook.get(name, []),
                    last_24h=counts, failures=failures))
        return {"webhooks": out}

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
                    viewer=admin.subject,  # results only for the run's own account
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
                viewer=admin.subject,
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

    # ---- accounts

    @router.post("/accounts")
    @_denied
    def create_account(request: Request, body: Any = Body(None), admin=Depends(require_admin)):
        _check_mutation(request, admin)
        payload = _validated(AccountCreate, body)
        if payload.phone:
            service.require_org_admin(admin.actor(), "Only administrators can set a phone number.")
            service.check_phone(payload.email, payload.phone)
        created = service.create_account(
            admin.actor(), email=payload.email, name=payload.name,
            password=payload.password.get_secret_value() if payload.password else None,
            sign_in=payload.sign_in,
            grants=[(g.hub, g.permission) for g in payload.grants],
        )
        if payload.phone:
            created.update(phone=service.set_phone(admin.actor(), payload.email,
                                                   payload.phone)["phone"])
        return dict(ok=True, **created)

    @router.post("/accounts/grant")
    @_denied
    def grant_existing_account(request: Request, payload: ExistingAccountGrant,
                               admin=Depends(require_admin)):
        """Give an existing account access: Add user's retry after a partial
        create, and API callers. Never creates an account."""
        _check_mutation(request, admin)
        result = service.grant_existing_account(
            admin.actor(), email=payload.email,
            grants=[(g.hub, g.permission) for g in payload.grants],
        )
        return dict(ok=True, **result)

    @router.get("/accounts/{subject}")
    @_denied
    def account_info(subject: str, admin=Depends(require_admin)):
        return service.account_info(admin.actor(), subject)

    @router.post("/accounts/{subject}/password")
    @_denied
    def reset_password(subject: str, request: Request, body: Any = Body(None),
                       admin=Depends(require_admin)):
        _check_mutation(request, admin)
        payload = _validated(LinkPasswordRequest if service.links_mode() else PasswordRequest, body)
        result = service.set_password(
            admin.actor(), subject,
            payload.password.get_secret_value() if payload.password else None)
        # Default mode without a password: the one-time link, shown once.
        return dict(ok=True, subject=normalize(subject), **(result or {}))

    @router.post("/accounts/{subject}/phone")
    @_denied
    def set_phone(subject: str, request: Request, payload: PhoneRequest,
                  admin=Depends(require_admin)):
        """The number a person's WhatsApp and Telegram messages come from.
        Organization administrators; an empty number clears it."""
        _check_mutation(request, admin)
        return dict(ok=True, **service.set_phone(admin.actor(), subject, payload.phone))

    @router.post("/accounts/{subject}/approve")
    @_denied
    def approve_account(subject: str, request: Request, admin=Depends(require_admin)):
        _check_mutation(request, admin)
        service.approve_account(admin.actor(), subject)
        return dict(ok=True, subject=normalize(subject))

    @router.post("/accounts/{subject}/role")
    @_denied
    def set_role(subject: str, request: Request, payload: RoleRequest,
                 admin=Depends(require_admin)):
        """User or Administrator: sets Hubzoid administration and the chat
        app's role together. A partial result is a 502 `role_partial` naming
        the side that is set; repeating the request finishes it."""
        _check_mutation(request, admin)
        return dict(ok=True, **service.set_role(admin.actor(), subject, payload.role))

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
        _check_same_origin(request)
        service.reject(actor, request_id)
        return dict(ok=True, id=request_id, status="rejected")

    _PERIODS = {"24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}

    @router.get("/summary")
    def summary(period: str = "7d", admin=Depends(require_admin)):
        """The Console home: usage per hub from Hubzoid's own tables (usage,
        access decisions, grants, DBOS runs, missed schedule slots), plus
        `user_accounts`, the login accounts in the viewer's scope read from the
        chat app's user directory (independent of `period`). A number Hubzoid
        cannot know is null ("unavailable"), never zero."""
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
            subjects = {g[0] for g in grants if g[1] == key and not g[0].startswith("workflow:")}
            r = runs.get(key, {"runs": 0, "failed": 0, "cancelled": 0}) if runs_ok else None
            rows.append(dict(
                key=key, name=h.get("name") or key,
                chats=u.get("chats", 0), messages=u.get("messages", 0),
                active_users=u.get("active_users", 0),
                input_tokens=u.get("input_tokens", 0), output_tokens=u.get("output_tokens", 0),
                cost_usd=u.get("cost_usd"), unpriced=u.get("unpriced", 0),
                last_activity=u.get("last_activity"),
                users_with_access=len(subjects - {"*"}),
                everyone="*" in subjects,
                denials=denials.get(normalize(Path(h["path"]).name), 0),
                has_workflows=key in work_keys,
                runs=r["runs"] if r and key in work_keys else None,
                failed=r["failed"] if r and key in work_keys else None,
                missed=missed_slots(h) if key in work_keys else None,
            ))
        # Login accounts the viewer is responsible for, from the chat app's user
        # directory. Not tied to the period, unlike `active_users`. A delegate
        # counts only accounts holding access in their own hubs.
        from .access.account_counts import user_account_summary
        from .access.service import Scope

        try:
            user_accounts = user_account_summary(hub_dir, scope=Scope(
                org_admin=admin.is_org_admin, hubs=tuple(h["key"] for h in hs)))
        except Exception:  # noqa: BLE001 — unknown, never zero
            log.exception("summary: account count unavailable")
            user_accounts = {"accounts": None, "hubs": len(hs)}

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
                    runs_available=runs_ok, totals=totals, hubs=rows,
                    user_accounts=user_accounts)

    @router.get("/overview")
    def overview(admin=Depends(require_admin)):
        gs = store_for(hub_dir)
        hs = allowed_hubs(admin)
        keys = {h["key"] for h in hs}
        grants = [g for g in gs.list_grants() if g[1] in keys]
        return dict(
            hubs=len(hs),
            grants=len(grants),
            people=len({g[0] for g in grants if g[0] != "*"}),
        )

    from . import portal_evals
    portal_evals.register(router, hub_dir, require_admin=require_admin, require_hub=require_hub)
    # Durable work items and their operations: any signed-in person, with the
    # requester-only and manager rules applied by the work-items service.
    from .work_items import routes as work_item_routes
    work_item_routes.register(router, require_person,
                              lambda actor, hub: deployment.hub_path(hub_dir, hub))
    work_item_routes.register_stations(router, require_person,
                                       lambda actor, hub: deployment.hub_path(hub_dir, hub))
    work_item_routes.register_budgets(router, require_person,
                                      lambda actor, hub: deployment.hub_path(hub_dir, hub))
    # Sandboxes: metadata for viewers, controls for managers.
    from .sandbox import routes as sandbox_routes
    sandbox_routes.register(router, require_person, lambda actor, hub: deployment.hub_path(hub_dir, hub))
    # Cards (MCP Apps): settings for administrators, instances for their requester.
    from .apps import routes as app_routes
    app_routes.register(router, require_person, lambda actor, hub: deployment.hub_path(hub_dir, hub))
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


def _has_connections(hub_dir: Path, account: str, email: str) -> bool:
    """Whether a person has connections to see: a connector they may connect,
    or one they connected (it can still be disconnected)."""
    from .connectors import per_user, registry, tokens

    try:
        ids = [c.id for c in registry.list_all(hub_dir) if c.enabled and c.auth_type != "shared"]
        return bool(per_user.allowed_ids(hub_dir, email, ids) or tokens.for_user(hub_dir, account))
    except Exception:  # noqa: BLE001 — the page still opens; the tab just isn't offered
        log.exception("portal: could not check connections for the people page")
        return False


def _tell_owner_link_off(hub_dir: Path, found) -> None:
    """Tell a personal report's owner an admin turned its company link off,
    when the hub emails people (`notify_by_email`)."""
    from . import email_delivery
    from .artifacts import domains
    from .workflows.identity import RunIdentity
    from .workflows.personal import email_on

    if not email_on(hub_dir):
        return
    try:
        email_delivery.send_to_owner(
            hub_dir, hub=found.hub, identity=RunIdentity(found.owner, None, "schedule", found.owner),
            subject=f"{found.title or found.key}: company link turned off",
            body=(f"An admin turned off the {domains.label()} link to your report. People who use "
                  "the agent can still open it.\n"),
            workflow=found.workflow, idem_key=f"series:{found.id}:off:{found.domain_changed}")
    except Exception:  # noqa: BLE001 — a notice never blocks the admin's change
        log.exception("portal: could not tell %s about a company link", found.owner)
