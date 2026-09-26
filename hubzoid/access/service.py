# Hubzoid access management. Apache-2.0 licensed like the rest of the repository.
"""The one authorization service for people, accounts and access changes.

Every Console route, every `/portal/api` caller holding an Open WebUI API key
and every agent tool goes through `AccessService`. Route handlers and tools
never decide authority themselves: they build an `Actor` from a verified
identity and call a method here. A different chat frontend would reuse this
module and replace only the identity check (`access.session`) and the account
directory (`access.accounts`).

Rules (checked on every write, from the store, never from the caller):
  * Organization administrators (org-wide `manage_access`) keep their scope:
    every registered agent, organization administrators, public access and
    account actions.
  * A hub delegate (per-hub `manage_access`) may change access only in hubs
    they manage, and only within their ceiling: their own current effective
    permissions in that hub, minus `manage_access`. The ceiling bounds grants
    and revokes alike (a `use_hub` revoke removes everything, so it is refused
    when the person holds something outside the ceiling). Delegates cannot
    grant `manage_access`, change their own access, change public access or an
    organization administrator, or make account-wide changes. A capability
    registered with `delegate_grantable=False` is outside every delegate's
    ceiling.
  * Only catalogue capabilities with an explicit grant can be granted. An
    `included` capability comes with `use_hub` and has no grant of its own; an
    obsolete grant (its capability is gone) can be removed, never granted.
  * Accounts are created with the chat-app role `user`. The password is never
    stored, logged, audited, returned or placed in a change request.
  * Agent tools only propose (`propose`). A change request applies after the
    same person confirms the exact plan (`confirm`) with a verified web
    session: single use, short lived, re-checked at confirmation, audited.
  * Store or directory errors deny (fail closed).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from sqlalchemy import text

from .. import capabilities, deployment
from .identity import normalize
from .store import (
    EVERYONE,
    MANAGE_ACCESS,
    ORG,
    USE_HUB,
    LastAdminError,
    RevisionConflict,
)

log = logging.getLogger("hubzoid.access")

#: Surfaces an Actor may carry. `console` is the Admin Console web session.
SURFACES = frozenset({"console", "owui", "web", "api", "mcp", "whatsapp", "telegram"})
#: Surfaces the proposal tools run on. Proposing is harmless and confirmation
#: needs a verified web session, so the messaging surfaces are included.
#: Anonymous, `workflow`, `system` and every Slack surface are refused.
TOOL_SURFACES = frozenset({"owui", "web", "api", "mcp", "whatsapp", "telegram"})
MAX_PENDING_REQUESTS = 20
# Refusals that happen before anything is written; a confirmation that hits one
# leaves its request pending so the manager can correct the input.
_RETRYABLE = frozenset({"rejected", "invalid_password"})
_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

LEGACY_MSG = (
    "This agent's access is still managed in the chat app — it has not been migrated "
    "to the dashboard. Migrate the agent first; edits made here would not take effect "
    "and would be overwritten by migration."
)
UNAVAILABLE_MSG = (
    "This account is unavailable in the chat app (awaiting approval or removed). "
    "Approve or restore it in Open WebUI, then refresh accounts."
)


@dataclass(frozen=True)
class Actor:
    """Who is acting. Built only from a verified identity: an Open WebUI session,
    an Open WebUI API key, or the bridge's trusted request identity. Never from
    a request body or a model argument."""

    subject: str  # normalized
    surface: str  # console|owui|web|api|mcp|whatsapp|telegram
    via: str  # "session" | "api-key" | "bridge"


@dataclass(frozen=True)
class Scope:
    org_admin: bool
    hubs: tuple[str, ...]  # hubs this actor may manage

    @property
    def any(self) -> bool:
        return self.org_admin or bool(self.hubs)


class Denied(Exception):
    """A refused request. `status` is an HTTP status, `code` a stable token and
    `message` safe to show (never a secret)."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def ttl() -> int:
    raw = os.environ.get("HUBZOID_CHANGE_REQUEST_TTL", "").strip()
    try:
        value = int(raw) if raw else 900
    except ValueError:
        value = 900
    return max(60, min(value, 86400))


def check_password(password: str | None) -> str:
    """The minimum rule applied before a password is sent to the chat app. The
    message never repeats the password."""
    if not isinstance(password, str) or not password:
        raise Denied(422, "invalid_password", "Enter a password.")
    if len(password) < 8:
        raise Denied(422, "invalid_password", "Use a password of at least 8 characters.")
    if len(password.encode("utf-8")) > 72:
        raise Denied(422, "invalid_password", "Use a password of at most 72 bytes.")
    return password


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def plan_hash(request_id: str, actor: str, plan: dict) -> str:
    return hashlib.sha256(
        _canonical({"id": request_id, "actor": actor, "plan": plan}).encode()
    ).hexdigest()


def _ops_from(grant: Iterable[str], revoke: Iterable[str]) -> list[tuple[str, str]]:
    # Revokes first, then grants, as the Console editor sends them.
    return [("revoke", normalize(p)) for p in revoke] + [("grant", normalize(p)) for p in grant]


class AccessService:
    """Authorization for Console, management API and agent tools. Cheap to
    construct; holds no per-request state."""

    def __init__(self, hub_dir: Path, *, accounts=None):
        self.hub_dir = Path(hub_dir)
        self._accounts_override = accounts

    # ---- plumbing -------------------------------------------------------------

    @property
    def store(self):
        from . import store_for

        try:
            return store_for(self.hub_dir)
        except Exception:  # noqa: BLE001
            log.exception("access service: store unavailable")
            raise Denied(503, "store_unavailable",
                         "Access data is unavailable. Try again shortly.")

    def accounts(self):
        """The account directory (Open WebUI admin API as the service account)."""
        if self._accounts_override is not None:
            return self._accounts_override
        from . import accounts as accountlib

        try:
            return accountlib.for_deployment(self.hub_dir)
        except accountlib.AccountError as exc:
            raise Denied(exc.status, exc.code, exc.message)

    def _hubs(self) -> list[dict]:
        try:
            return deployment.hubs(self.hub_dir)
        except Exception:  # noqa: BLE001
            log.exception("access service: deployment unreadable")
            raise Denied(503, "deployment_unavailable",
                         "The deployment configuration is unavailable. Try again shortly.")

    def _hub_path(self, hub: str) -> Path:
        for h in self._hubs():
            if h["key"] == hub:
                return Path(h["path"])
        raise Denied(404, "unknown_hub", "Hub is not registered in this deployment")

    def catalog(self, hub: str) -> list[dict]:
        """Every capability this hub offers (`capabilities.catalog`): built-ins,
        connectors and restricted tools, plus granted ids that no longer exist,
        marked obsolete so they stay visible and removable."""
        hub = normalize(hub)
        path = self._hub_path(hub)
        try:
            granted = {p for (_s, _h, p) in self.store.list_grants(hub)}
        except Exception:  # noqa: BLE001 — without the store nothing is obsolete-listed
            log.warning("access service: grants unreadable while listing %s", hub)
            granted = set()
        return capabilities.catalog(path, granted=granted)

    def _audit(self, actor: Actor | str, action: str, **fields) -> None:
        who = actor.subject if isinstance(actor, Actor) else actor
        if isinstance(actor, Actor):
            fields.setdefault("surface", actor.surface)
        self.store.audit_event(who, action, **fields)

    # ---- who may do what --------------------------------------------------------

    def scope(self, actor: Actor) -> Scope:
        """The hubs `actor` manages, from the store. Blocked or unknown actors,
        workflow identities and the public wildcard manage nothing."""
        subject = normalize(actor.subject)
        if not subject or subject == EVERYONE or subject.startswith("workflow:"):
            return Scope(False, ())
        gs = self.store
        hubs = self._hubs()
        try:
            org = gs.can(subject, ORG, MANAGE_ACCESS)
            managed = tuple(
                h["key"] for h in hubs if org or gs.can(subject, h["key"], MANAGE_ACCESS)
            )
        except Exception:  # noqa: BLE001
            log.exception("access service: scope check failed")
            raise Denied(503, "store_unavailable",
                         "Access data is unavailable. Try again shortly.")
        return Scope(org, managed)

    def _require_scope(self, actor: Actor) -> Scope:
        scope = self.scope(actor)
        if not scope.any:
            raise Denied(403, "forbidden",
                         "Sign in with an account allowed to manage agent access.")
        return scope

    def require_org_admin(self, actor: Actor, message: str = "Organization admin required") -> Scope:
        scope = self.scope(actor)
        if not scope.org_admin:
            raise Denied(403, "org_admin_required", message)
        return scope

    def ceiling(self, actor: Actor, hub: str) -> frozenset[str]:
        """What `actor` may grant or revoke in `hub`. An organization
        administrator: every capability in the catalog. A delegate: their own
        current effective permissions there, minus `manage_access`."""
        hub = normalize(hub)
        scope = self._require_scope(actor)
        return self._ceiling(actor, scope, hub)

    def _ceiling(self, actor: Actor, scope: Scope, hub: str) -> frozenset[str]:
        if not scope.org_admin and hub not in scope.hubs:
            raise Denied(403, "forbidden", f"Cannot manage {hub}")
        entries = self.catalog(hub)
        obsolete = {e["permission"] for e in entries if e.get("obsolete")}
        grantable = {e["permission"] for e in entries if _grantable(e)}
        if scope.org_admin:
            return frozenset(grantable | obsolete)
        delegable = {e["permission"] for e in entries
                     if _grantable(e) and e.get("delegate_grantable", True)}
        try:
            held = self.store.permissions_for(actor.subject, hub)
        except Exception:  # noqa: BLE001
            raise Denied(503, "store_unavailable",
                         "Access data is unavailable. Try again shortly.")
        # An obsolete grant can only be removed, and only by someone who holds it.
        return frozenset((held & (delegable | obsolete)) - {MANAGE_ACCESS})

    def grantable(self, actor: Actor) -> dict[str, list[str]]:
        """{hub: [permission]} this actor may grant, for every hub they manage.
        A hub whose access is still in the chat app (legacy) grants nothing."""
        scope = self.scope(actor)
        out: dict[str, list[str]] = {}
        gs = self.store
        for hub in scope.hubs:
            if not gs.is_authoritative(hub):
                out[hub] = []
                continue
            out[hub] = sorted(self._ceiling(actor, scope, hub))
        return out

    def can_create_accounts(self, actor: Actor) -> bool:
        scope = self.scope(actor)
        if scope.org_admin:
            return True
        gs = self.store
        return any(gs.is_authoritative(h) for h in scope.hubs)

    # ---- access changes -----------------------------------------------------------

    def _check_ops(self, actor: Actor, scope: Scope, subject: str, hub: str,
                   ops: list[tuple[str, str]], *, new_account: bool = False
                   ) -> list[tuple[str, str]]:
        """Validate one subject's change set in one hub. Returns normalized ops.
        Order of checks is part of the API contract (status codes).
        `new_account`: the grants accompany an account being created now, whose
        binding clears a stale "unavailable" marker in the same transaction."""
        gs = self.store
        subject, hub = normalize(subject), normalize(hub)
        ops = [(a, normalize(p)) for a, p in ops]
        if not subject:
            raise Denied(422, "invalid_subject", "a person or service is required")
        if any(a not in ("grant", "revoke") for a, _ in ops):
            raise Denied(422, "invalid_action", "Each change must grant or revoke")
        if hub == ORG:
            if not scope.org_admin or subject == EVERYONE or any(
                p != MANAGE_ACCESS for _, p in ops
            ):
                raise Denied(403, "forbidden",
                             "Only organization admins can manage organization administrators")
            return ops
        if not scope.org_admin and hub not in scope.hubs:
            raise Denied(403, "forbidden", f"Cannot manage {hub}")
        self._hub_path(hub)
        if not gs.is_authoritative(hub):
            raise Denied(409, "legacy", LEGACY_MSG)
        entries = {e["permission"]: e for e in self.catalog(hub)}
        existing = None
        for action, p in ops:
            entry = entries.get(p)
            if entry is not None and _grantable(entry):
                continue
            if existing is None:
                existing = set(gs.list_grants(hub))
            # A removed or renamed tool can still be revoked, never granted.
            if action == "revoke" and (subject, hub, p) in existing:
                continue
            if entry is not None and entry.get("default") == "included":
                raise Denied(422, "included",
                             f"{entry['label']} comes with Use this agent; it has no grant of its own.")
            raise Denied(422, "unknown_permission", "Unknown permission for this hub")
        if subject == EVERYONE and not (
            scope.org_admin and all(p == USE_HUB for _, p in ops)
        ):
            raise Denied(403, "forbidden", "Only organization admins may change public hub access")
        if not scope.org_admin:
            self._check_delegate(actor, scope, subject, hub, ops)
        if any(a == "grant" for a, _ in ops):
            flags = _account_flags(gs, subject)
            if flags["suspended"]:
                raise Denied(409, "blocked", "Reactivate this user before granting access")
            if flags["account_unavailable"] and not new_account:
                raise Denied(409, "unavailable", UNAVAILABLE_MSG)
        return ops

    def _check_delegate(self, actor: Actor, scope: Scope, subject: str, hub: str,
                        ops: list[tuple[str, str]]) -> None:
        gs = self.store
        target_manages = gs.can(subject, hub, MANAGE_ACCESS)
        for action, p in ops:
            if p == MANAGE_ACCESS or (action == "revoke" and p == USE_HUB and target_manages):
                raise Denied(403, "forbidden",
                             "Only organization admins may change administrator access")
        if subject == normalize(actor.subject):
            raise Denied(403, "self_change",
                         "You can't change your own access. Ask an organization administrator.")
        if gs.can(subject, ORG, MANAGE_ACCESS):
            raise Denied(403, "forbidden",
                         "Only organization admins may change an organization administrator's access")
        ceiling = self._ceiling(actor, scope, hub)
        outside = sorted({p for _, p in ops} - ceiling)
        if outside:
            raise Denied(
                403, "outside_ceiling",
                "Outside your access: you can only grant or remove capabilities you hold in "
                f"{hub}. Not yours: {', '.join(outside)}.",
            )
        if any(a == "revoke" and p == USE_HUB for a, p in ops):
            held = {p for (s, h, p) in gs.list_grants(hub) if s == subject}
            beyond = sorted(held - ceiling - {USE_HUB})
            if beyond:
                raise Denied(
                    403, "outside_ceiling",
                    "Outside your access: removing all access would also remove capabilities "
                    f"you do not hold ({', '.join(beyond)}). Ask an organization administrator.",
                )

    def apply_access_change(self, actor: Actor, subject: str, hub: str,
                            ops: list[tuple[str, str]], *,
                            expected_revision: int | None = None,
                            request_id: str | None = None) -> int:
        """Check and apply one subject's change set in one hub atomically.
        The authority check and the write describe the same policy revision:
        when the caller gave none, a concurrent change re-runs the check."""
        gs = self.store
        for _attempt in range(3):
            scope = self._require_scope(actor)
            revision = gs.revision() if expected_revision is None else expected_revision
            checked = self._check_ops(actor, scope, subject, hub, ops)
            try:
                return gs.apply_changes(
                    normalize(subject), normalize(hub), checked,
                    expected_revision=revision,
                    actor=actor.subject, surface=actor.surface, request_id=request_id,
                )
            except RevisionConflict as exc:
                if expected_revision is not None:
                    raise Denied(409, "conflict", str(exc))
            except (ValueError, LastAdminError) as exc:
                raise Denied(409, "conflict", str(exc))
        raise Denied(409, "conflict",
                     "Access kept changing while saving. Reload and try again.")

    def set_blocked(self, actor: Actor, subject: str, suspended: bool) -> bool:
        """Block or reactivate (organization administrators). Returns whether the
        admin marker changed; reactivating a subject that is not admin-blocked
        writes nothing."""
        self.require_org_admin(actor)
        gs = self.store
        subject = normalize(subject)
        if not subject or subject == EVERYONE:
            raise Denied(409, "invalid_subject", "a person or service is required")
        before = _account_flags(gs, subject)
        changed = suspended or before["suspended"]
        if changed:
            try:
                gs.suspend(subject, actor=actor.subject, suspended=suspended,
                           surface=actor.surface)
            except (LastAdminError, ValueError) as exc:
                raise Denied(409, "conflict", str(exc))
        return changed

    def refresh_accounts(self, actor: Actor) -> int:
        """Re-read the chat app's account directory (organization administrators)."""
        self.require_org_admin(actor)
        from .owui import directory

        try:
            rows = directory(self.hub_dir)
            self.store.reconcile_accounts(
                [dict(id=r["owui_id"], email=r["email"], name=r["display"], role=r["role"])
                 for r in rows]
            )
        except Exception:  # noqa: BLE001
            log.exception("OWUI directory refresh failed")
            raise Denied(503, "accounts_unavailable",
                         "Account refresh failed. Check OWUI service credentials and logs.")
        return len(rows)

    def sync_visibility(self, actor: Actor) -> dict:
        self.require_org_admin(actor)
        from .reconcile import sync_owui

        return sync_owui(self.hub_dir)

    # ---- accounts ---------------------------------------------------------------

    def _account_target(self, actor: Actor, subject: str) -> dict:
        """Common guard for account-wide actions (organization administrators)."""
        self.require_org_admin(
            actor, "Only organization administrators can change chat accounts.")
        subject = normalize(subject)
        if not subject or subject == EVERYONE or subject.startswith("workflow:"):
            raise Denied(422, "invalid_subject", "Choose a person's account.")
        if subject == normalize(actor.subject):
            raise Denied(409, "self_change",
                         "Change your own account in the chat app's settings.")
        from .accounts import service_account_email

        if subject == service_account_email(self.hub_dir):
            raise Denied(409, "service_account",
                         "This is the Console's service account. Change it on the server.")
        identity = self.store.identity(subject) or {}
        if not identity.get("owui_id"):
            raise Denied(409, "no_account",
                         "No chat account is linked to this person yet. Refresh accounts first.")
        return identity

    def _live_account(self, directory, identity: dict) -> dict:
        from .accounts import AccountError

        try:
            account = directory.get(identity["owui_id"])
        except AccountError as exc:
            raise Denied(exc.status, exc.code, exc.message)
        if not account or normalize(account.get("email") or "") != identity["subject"]:
            raise Denied(409, "account_changed",
                         "The chat account linked to this person changed or was removed. "
                         "Refresh accounts and try again.")
        return account

    def _directory_call(self, fn, *args, **kwargs):
        from .accounts import AccountError

        try:
            return fn(*args, **kwargs)
        except AccountError as exc:
            raise Denied(exc.status, exc.code, exc.message)

    def create_account(self, actor: Actor, *, email: str, name: str, password: str,
                       grants: list[tuple[str, str]], request_id: str | None = None) -> dict:
        """Create a chat login (role `user`), bind it and apply initial grants.

        Everything that can be checked is checked before the chat app is
        called. If binding or granting then fails, the new account is deleted
        (it has no chats); if that also fails the error says so plainly."""
        email = normalize(email)
        if not _EMAIL.match(email) or len(email) > 320:
            raise Denied(422, "invalid_email", "Enter a valid email address.")
        name = (name or "").strip()
        if not name or len(name) > 200:
            raise Denied(422, "invalid_name", "Enter the person's name.")
        check_password(password)
        by_hub = self._group_grants(grants)
        scope, replace = self._check_new_account(actor, email, by_hub)
        directory = self.accounts()
        created = self._directory_call(
            directory.create, email=email, name=name, password=password, role="user")
        if normalize(created.get("email") or "") != email or created.get("role") != "user":
            self._remove_new(directory, created)
            raise Denied(502, "chat_app_error",
                         "The chat app created an unexpected account; it was removed.")
        gs = self.store
        try:
            revision = None
            for _attempt in range(3):
                scope = self._require_scope(actor)
                base = gs.revision()
                planned = {
                    hub: self._check_ops(actor, scope, email, hub,
                                         [("grant", p) for p in sorted(perms)], new_account=True)
                    for hub, perms in by_hub.items()
                }
                try:
                    revision = gs.bind_new_account(
                        email, owui_id=created["id"], display=name, grants=planned,
                        actor=actor.subject, expected_revision=base, replace=replace,
                        surface=actor.surface, request_id=request_id,
                    )
                    break
                except RevisionConflict:
                    continue
            if revision is None:
                raise Denied(409, "conflict",
                             "Access kept changing while saving. Review and try again.")
        except Exception as exc:  # noqa: BLE001 — compensate, then report honestly
            removed = self._remove_new(directory, created)
            base_msg = exc.message if isinstance(exc, Denied) else "Access could not be granted."
            if not isinstance(exc, Denied):
                log.exception("account create: binding failed for a new account")
            try:
                self._audit(actor, "account_create_failed", subject=email, hub=ORG,
                            request_id=request_id)
            except Exception:  # noqa: BLE001
                log.warning("account create: failure could not be audited")
            if removed:
                status = exc.status if isinstance(exc, Denied) else 503
                code = exc.code if isinstance(exc, Denied) else "store_unavailable"
                raise Denied(status, code,
                             f"{base_msg} The new chat account was removed, so nothing changed.")
            raise Denied(
                502, "partial",
                f"{base_msg} The chat account {email} was created but could not be removed "
                "automatically. Delete it under People or in the chat app, then try again.",
            )
        return dict(
            subject=email, owui_id=created["id"], name=name, role="user",
            grants={h: sorted(p) for h, p in by_hub.items()}, revision=revision,
        )

    def _group_grants(self, grants: list[tuple[str, str]]) -> dict[str, set[str]]:
        by_hub: dict[str, set[str]] = {}
        for hub, perm in grants or []:
            hub, perm = normalize(hub), normalize(perm)
            if not hub or not perm:
                raise Denied(422, "invalid_grant", "Each grant needs an agent and a capability")
            by_hub.setdefault(hub, set()).add(perm)
        return by_hub

    def _check_new_account(self, actor: Actor, email: str,
                           by_hub: dict[str, set[str]]) -> tuple[Scope, bool]:
        """Everything that must hold before a new account is created. Returns
        (scope, replace) where replace means re-creating an email whose earlier
        account is gone (organization administrators only)."""
        scope = self._require_scope(actor)
        if not scope.org_admin:
            if not any(self.store.is_authoritative(h) for h in scope.hubs):
                raise Denied(403, "forbidden",
                             "Creating accounts needs an agent whose access you manage here.")
            if not by_hub:
                raise Denied(422, "grant_required",
                             "Choose access in at least one agent you manage.")
        gs = self.store
        flags = _account_flags(gs, email)
        if flags["suspended"]:
            raise Denied(409, "blocked",
                         "This person is blocked. Reactivate them under People first.")
        identity = gs.identity(email) or {}
        replace = False
        if identity.get("owui_id"):
            if identity.get("pending"):
                raise Denied(409, "account_exists",
                             "This person already signed up and is awaiting approval. "
                             "Approve the account instead.")
            if not flags["account_unavailable"]:
                raise Denied(409, "account_exists",
                             "An account with this email already exists. Grant access instead.")
            if not scope.org_admin:
                raise Denied(409, "account_replaced",
                             "This email belonged to an earlier chat account. Ask an "
                             "organization administrator to re-create it.")
            replace = True
        if not scope.org_admin:
            # Whoever sets the password can use every grant already waiting for
            # this email, so a delegate may create it only when all of those are
            # within their own ceiling.
            ceilings: dict[str, frozenset[str]] = {}
            for _s, hub, perm in (g for g in gs.list_grants() if g[0] == email):
                if hub == ORG or hub not in scope.hubs:
                    beyond = True
                else:
                    if hub not in ceilings:
                        ceilings[hub] = self._ceiling(actor, scope, hub)
                    beyond = perm not in ceilings[hub]
                if beyond:
                    raise Denied(403, "outside_ceiling",
                                 "This email already holds access you don't manage. Ask an "
                                 "organization administrator to create the account.")
        for hub, perms in by_hub.items():
            self._check_ops(actor, scope, email, hub, [("grant", p) for p in sorted(perms)],
                            new_account=True)
        return scope, replace

    def _remove_new(self, directory, created: dict) -> bool:
        try:
            directory.delete(created["id"])
            return True
        except Exception:  # noqa: BLE001
            log.error("account create: could not remove a partially created account")
            return False

    def account_info(self, actor: Actor, subject: str) -> dict:
        """The chat account behind a person, read live (organization administrators)."""
        identity = self._account_target(actor, subject)
        account = self._live_account(self.accounts(), identity)
        return dict(subject=identity["subject"], name=account.get("name"),
                    role=account.get("role"))

    def set_password(self, actor: Actor, subject: str, password: str) -> None:
        identity = self._account_target(actor, subject)
        check_password(password)
        directory = self.accounts()
        self._live_account(directory, identity)
        self._directory_call(directory.update, identity["owui_id"], password=password)
        self._audit(actor, "account_password_reset", subject=identity["subject"], hub=ORG)

    def approve_account(self, actor: Actor, subject: str) -> None:
        identity = self._account_target(actor, subject)
        directory = self.accounts()
        account = self._live_account(directory, identity)
        if account.get("role") != "pending":
            raise Denied(409, "not_pending", "This account is not awaiting approval.")
        self._directory_call(directory.update, identity["owui_id"], role="user")
        self.store.upsert_identity(email=identity["subject"], owui_id=identity["owui_id"],
                                   pending=False)
        self._audit(actor, "account_approve", subject=identity["subject"], hub=ORG)

    def set_chat_role(self, actor: Actor, subject: str, role: str) -> None:
        role = normalize(role)
        if role not in ("user", "admin"):
            raise Denied(422, "invalid_role", "The chat-app role must be user or admin.")
        identity = self._account_target(actor, subject)
        directory = self.accounts()
        account = self._live_account(directory, identity)
        if account.get("role") == "pending":
            raise Denied(409, "pending", "Approve this account first.")
        if account.get("role") == role:
            return
        self._directory_call(directory.update, identity["owui_id"], role=role)
        self._audit(actor, "account_role", subject=identity["subject"], hub=ORG, permission=role)

    def delete_account(self, actor: Actor, subject: str) -> None:
        """Remove every grant, then the chat account. The last organization
        administrator cannot be deleted."""
        identity = self._account_target(actor, subject)
        directory = self.accounts()
        self._live_account(directory, identity)
        gs = self.store
        try:
            gs.revoke_all(identity["subject"], actor=actor.subject, surface=actor.surface)
        except (LastAdminError, ValueError) as exc:
            raise Denied(409, "last_admin", str(exc))
        try:
            directory.delete(identity["owui_id"])
        except Exception as exc:  # noqa: BLE001
            detail = getattr(exc, "message", "")
            raise Denied(502, "partial",
                         "Access was removed, but the chat account could not be deleted. "
                         f"{detail} Try again.".strip())
        gs.mark_account_removed(identity["subject"], actor=actor.subject, surface=actor.surface)

    # ---- change requests --------------------------------------------------------

    def propose(self, actor: Actor, plan: dict) -> dict:
        """Validate a proposed change against the actor's current authority and
        store it for confirmation. Nothing changes until `confirm`."""
        if actor.surface not in SURFACES or not normalize(actor.subject):
            raise Denied(403, "surface", "Access changes can't be proposed from here.")
        scope = self._require_scope(actor)
        canonical, kind, hub, target = self._canonical_plan(actor, scope, plan)
        gs = self.store
        now = time.time()
        engine = gs.engine
        subject = normalize(actor.subject)
        self._expire(now, actor=subject)
        with engine.begin() as conn:
            pending = conn.execute(
                text("SELECT count(*) FROM hz_change_requests "
                     "WHERE actor=:a AND status='pending' AND expires >= :now"),
                {"a": subject, "now": now},
            ).scalar() or 0
            if pending >= MAX_PENDING_REQUESTS:
                raise Denied(429, "too_many",
                             f"You have {MAX_PENDING_REQUESTS} changes waiting for confirmation. "
                             "Confirm or reject some in the Admin Console first.")
            request_id = secrets.token_urlsafe(24)
            expires = now + ttl()
            conn.execute(
                text(
                    "INSERT INTO hz_change_requests (id, created, expires, actor, surface, kind, "
                    "hub, target, plan, plan_hash, base_revision, status) VALUES "
                    "(:id, :c, :e, :a, :s, :k, :h, :t, :p, :ph, :r, 'pending')"
                ),
                {"id": request_id, "c": now, "e": expires, "a": subject, "s": actor.surface,
                 "k": kind, "h": hub, "t": target, "p": _canonical(canonical),
                 "ph": plan_hash(request_id, subject, canonical), "r": gs.revision()},
            )
            gs.write_audit(conn, subject, "change_proposed", subject=target, hub=hub,
                           surface=actor.surface, request_id=request_id)
        return dict(id=request_id, expires=expires, summary=self.summary(canonical),
                    confirm_path=f"/portal/#/confirm/{request_id}")

    def _canonical_plan(self, actor: Actor, scope: Scope, plan: dict):
        if not isinstance(plan, dict):
            raise Denied(422, "invalid_plan", "A change plan is required.")
        kind = plan.get("kind")
        hub = normalize(plan.get("hub") or "")
        if not hub or hub == ORG:
            raise Denied(422, "invalid_plan", "Name the agent the change applies to.")

        def names(key):
            values = plan.get(key) or []
            if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                raise Denied(422, "invalid_plan", f"{key} must be a list of capability names")
            return sorted({normalize(v) for v in values if normalize(v)})

        grant, revoke = names("grant"), names("revoke")
        if kind == "access":
            subject = normalize(plan.get("subject") or "")
            if not subject:
                raise Denied(422, "invalid_plan", "Name the person whose access changes.")
            if not grant and not revoke:
                raise Denied(422, "invalid_plan", "The change grants or removes nothing.")
            if set(grant) & set(revoke):
                raise Denied(422, "invalid_plan", "A capability can't be both granted and removed.")
            self._check_ops(actor, scope, subject, hub, _ops_from(grant, revoke))
            return (dict(kind="access", hub=hub, subject=subject, grant=grant, revoke=revoke),
                    "access", hub, subject)
        if kind == "account":
            email = normalize(plan.get("email") or "")
            name = (plan.get("name") or "").strip()
            if not _EMAIL.match(email) or len(email) > 320:
                raise Denied(422, "invalid_email", "Enter a valid email address.")
            if not name or len(name) > 200:
                raise Denied(422, "invalid_name", "Enter the person's name.")
            if revoke:
                raise Denied(422, "invalid_plan", "A new account has nothing to remove.")
            grant = sorted(set(grant) | {USE_HUB})
            self._check_new_account(actor, email, {hub: set(grant)})
            return (dict(kind="account", hub=hub, email=email, name=name, grant=grant),
                    "account", hub, email)
        raise Denied(422, "invalid_plan", "Unknown kind of change.")

    def summary(self, plan: dict) -> str:
        try:
            labels = {p["permission"]: p["label"] for p in self.catalog(plan["hub"])}
        except Denied:
            labels = {}

        def lab(ps):
            return ", ".join(labels.get(p, p) for p in ps)

        if plan["kind"] == "account":
            return (f"Create a chat account for {plan['name']} <{plan['email']}> with access to "
                    f"{plan['hub']}: {lab(plan['grant'])}.")
        parts = []
        if plan["grant"]:
            parts.append(f"allow {lab(plan['grant'])}")
        if plan["revoke"]:
            parts.append(f"remove {lab(plan['revoke'])}")
        return f"For {plan['subject']} in {plan['hub']}: " + "; ".join(parts) + "."

    def _row(self, request_id: str) -> dict | None:
        if not isinstance(request_id, str) or not _REQUEST_ID.match(request_id):
            return None
        with self.store.engine.connect() as conn:
            row = conn.execute(
                text("SELECT id, created, expires, actor, surface, kind, hub, target, plan, "
                     "plan_hash, base_revision, status, decided, decided_by, result "
                     "FROM hz_change_requests WHERE id=:id"),
                {"id": request_id},
            ).mappings().fetchone()
        return dict(row) if row else None

    def _own_row(self, actor: Actor, request_id: str) -> dict:
        row = self._row(request_id)
        # Someone else's request is indistinguishable from a missing one.
        if not row or row["actor"] != normalize(actor.subject):
            raise Denied(404, "not_found", "This change request doesn't exist or isn't yours.")
        return row

    def _expire(self, now: float, *, actor: str | None = None,
                request_id: str | None = None) -> None:
        """Mark overdue pending requests expired, one audit row each."""
        gs = self.store
        where = "status='pending' AND expires < :now"
        params: dict = {"now": now}
        if actor:
            where += " AND actor=:a"
            params["a"] = actor
        if request_id:
            where += " AND id=:id"
            params["id"] = request_id
        with gs.engine.begin() as conn:
            rows = conn.execute(
                text(f"SELECT id, actor, surface, hub, target FROM hz_change_requests WHERE {where}"),
                params,
            ).fetchall()
            for rid, who, surface, hub, target in rows:
                done = conn.execute(
                    text("UPDATE hz_change_requests SET status='expired', decided=:now "
                         "WHERE id=:id AND status='pending'"),
                    {"id": rid, "now": now},
                ).rowcount
                if done:
                    gs.write_audit(conn, who, "change_expired", subject=target, hub=hub,
                                   surface=surface, request_id=rid)

    def get_request(self, actor: Actor, request_id: str) -> dict:
        row = self._own_row(actor, request_id)
        now = time.time()
        if row["status"] == "pending" and row["expires"] < now:
            self._expire(now, request_id=row["id"])
            row = self._own_row(actor, request_id)
        plan = json.loads(row["plan"])
        view = dict(
            id=row["id"], status=row["status"], kind=row["kind"], hub=row["hub"],
            target=row["target"], surface=row["surface"], created=row["created"],
            expires=row["expires"], decided=row["decided"], plan=plan,
            plan_hash=row["plan_hash"], summary=self.summary(plan),
            result=row["result"] if row["status"] in ("confirmed", "failed") else None,
        )
        try:
            view["labels"] = {p["permission"]: p for p in self.catalog(row["hub"])}
        except Denied:
            view["labels"] = {}
        view["hub_name"] = next(
            (h.get("name") or h["key"] for h in self._hubs() if h["key"] == row["hub"]),
            row["hub"])
        if plan["kind"] == "access":
            view["current"] = sorted(
                p for (s, h, p) in self.store.list_grants(row["hub"]) if s == plan["subject"])
        view["problem"] = None
        if row["status"] == "pending":
            try:
                scope = self._require_scope(actor)
                self._canonical_plan(actor, scope, plan)
            except Denied as exc:
                view["problem"] = exc.message
        return view

    def confirm(self, actor: Actor, request_id: str, *, plan_hash: str,
                password: str | None = None) -> dict:
        """Apply a pending request exactly as proposed. Only its proposer, with
        a verified web session, the unchanged plan hash and before it expires;
        claimed atomically so it applies at most once; re-checked now."""
        if actor.via != "session":
            raise Denied(403, "session_required",
                         "Confirm changes in the Admin Console while signed in.")
        row = self._own_row(actor, request_id)
        now = time.time()
        if row["status"] == "pending" and row["expires"] < now:
            self._expire(now, request_id=row["id"])
            raise Denied(410, "expired", "This request expired. Ask again for a new one.")
        if row["status"] != "pending":
            raise Denied(409, "not_pending", f"This request was already {row['status']}.")
        if not isinstance(plan_hash, str) or not hmac.compare_digest(plan_hash, row["plan_hash"]):
            raise Denied(409, "plan_changed",
                         "This request doesn't match what you reviewed. Reload it and review again.")
        plan = json.loads(row["plan"])
        if plan["kind"] == "account":
            check_password(password)
        gs = self.store
        with gs.engine.begin() as conn:
            claimed = conn.execute(
                text("UPDATE hz_change_requests SET status='applying' "
                     "WHERE id=:id AND status='pending' AND expires >= :now"),
                {"id": row["id"], "now": now},
            ).rowcount
        if claimed != 1:
            raise Denied(409, "not_pending", "This request was already used.")
        # Grants carry the surface the change was proposed from, and the request id.
        applier = Actor(subject=normalize(actor.subject), surface=row["surface"] or actor.surface,
                        via=actor.via)
        try:
            if plan["kind"] == "account":
                outcome = self.create_account(
                    applier, email=plan["email"], name=plan["name"], password=password,
                    grants=[(plan["hub"], p) for p in plan["grant"]], request_id=row["id"])
                result = dict(subject=outcome["subject"], grants=outcome["grants"])
            else:
                revision = self.apply_access_change(
                    applier, plan["subject"], plan["hub"],
                    _ops_from(plan["grant"], plan["revoke"]), request_id=row["id"])
                result = dict(subject=plan["subject"], revision=revision)
        except Exception as exc:  # noqa: BLE001
            message = exc.message if isinstance(exc, Denied) else "The change could not be applied."
            if isinstance(exc, Denied) and exc.code in _RETRYABLE:
                # The chat app refused the password before creating anything:
                # nothing was applied, so the request stays usable.
                self._unclaim(row)
                raise
            if not isinstance(exc, Denied):
                log.exception("change request %s failed", row["id"])
            self._finish(row, "failed", actor, message, "change_failed")
            if isinstance(exc, Denied):
                raise
            raise Denied(503, "failed", message)
        self._finish(row, "confirmed", actor, _canonical(result), "change_confirmed")
        return dict(id=row["id"], status="confirmed", result=result)

    def _unclaim(self, row: dict) -> None:
        with self.store.engine.begin() as conn:
            conn.execute(
                text("UPDATE hz_change_requests SET status='pending' "
                     "WHERE id=:id AND status='applying'"),
                {"id": row["id"]},
            )

    def _finish(self, row: dict, status: str, actor: Actor, result: str, action: str) -> None:
        gs = self.store
        now = time.time()
        with gs.engine.begin() as conn:
            conn.execute(
                text("UPDATE hz_change_requests SET status=:st, decided=:now, decided_by=:by, "
                     "result=:r WHERE id=:id"),
                {"st": status, "now": now, "by": normalize(actor.subject), "r": result[:1000],
                 "id": row["id"]},
            )
            gs.write_audit(conn, normalize(actor.subject), action, subject=row["target"],
                           hub=row["hub"], surface=actor.surface, request_id=row["id"])

    def reject(self, actor: Actor, request_id: str) -> None:
        row = self._own_row(actor, request_id)
        now = time.time()
        if row["status"] == "pending" and row["expires"] < now:
            self._expire(now, request_id=row["id"])
            raise Denied(410, "expired", "This request already expired.")
        gs = self.store
        with gs.engine.begin() as conn:
            done = conn.execute(
                text("UPDATE hz_change_requests SET status='rejected', decided=:now, "
                     "decided_by=:by WHERE id=:id AND status='pending'"),
                {"id": row["id"], "now": now, "by": normalize(actor.subject)},
            ).rowcount
            if done != 1:
                raise Denied(409, "not_pending", f"This request was already {row['status']}.")
            gs.write_audit(conn, normalize(actor.subject), "change_rejected",
                           subject=row["target"], hub=row["hub"], surface=actor.surface,
                           request_id=row["id"])


def _grantable(entry: dict) -> bool:
    """A catalogue entry that can be granted: current and not included."""
    return not entry.get("obsolete") and entry.get("default", "grant") != "included"


def _account_flags(gs, subject: str) -> dict:
    """The store's two block markers, read separately. `blocked` is their OR."""
    subject = normalize(subject)
    with gs.engine.connect() as conn:
        suspended = gs._meta_get(conn, "suspended:" + subject) == "1"  # noqa: SLF001
        unavailable = gs._meta_get(conn, "account_unavailable:" + subject) == "1"  # noqa: SLF001
    return dict(suspended=suspended, account_unavailable=unavailable,
                blocked=suspended or unavailable)
