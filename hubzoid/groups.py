"""Groups: people who hold agent access together.

A group is a named set of people, recorded by normalized email, that can be
given access like a person: a grant to ``group:<id>`` applies to every member
(``GrantStore.can`` checks the email, then its groups, then ``*``).
Organization administrators manage groups in the Console
(``/portal/api/groups``, registered by ``hubzoid.webapp_gateway``).

Rules (checked here and again in the store, which owns the writes):

  * Names are unique, compared case-insensitively, 1 to 100 characters, with no
    commas or semicolons. On an agent whose access is not yet managed in the
    Console, a group's name is also the restricted-tool permission it confers
    (``access.effective_groups``), and those lists are comma separated.
  * Members are email addresses. A member needs no account yet: access applies
    once they sign in with that email. Blocking a person (``store.suspend``)
    also stops their group access.
  * A group holds agent access only: entry and capabilities in a named agent,
    never Manage access. Administration stays per person.
  * Deleting a group removes every grant it holds and every membership.
  * Every change is one transaction that is audited and bumps the policy
    revision, so every bridge sees it on its next decision.
  * Store errors refuse (fail closed).
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterable

from .access.identity import normalize
from .access.store import (
    GROUP_PREFIX,
    GroupError,
    GroupNameTaken,
    GroupNotFound,
    group_id_of,
    group_subject,
    is_group_subject,
)

log = logging.getLogger("hubzoid.groups")

MAX_NAME = 100
MAX_DESCRIPTION = 500
MAX_EMAILS = 200
_EMAIL = re.compile(r"^[^\s@,;]+@[^\s@,;]+$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

#: ``update(description=UNSET)`` leaves the description as it is.
UNSET = object()

__all__ = [
    "GROUP_PREFIX",
    "GroupRefused",
    "GroupService",
    "UNSET",
    "group_id_of",
    "group_subject",
    "groups_of",
    "is_group_subject",
    "names_for",
]


class GroupRefused(Exception):
    """A refused group request: HTTP `status`, a stable `code` and a `message`
    safe to show (never a secret)."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def clean_name(raw) -> str:
    name = " ".join(str(raw or "").split())
    if not name:
        raise GroupRefused(422, "invalid_name", "Enter a group name.")
    if len(name) > MAX_NAME:
        raise GroupRefused(422, "invalid_name", f"Use a group name of at most {MAX_NAME} characters.")
    if "," in name or ";" in name or _CONTROL.search(name):
        raise GroupRefused(422, "invalid_name", "Group names can't contain commas or semicolons.")
    return name


def clean_description(raw) -> str | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if _CONTROL.search(text.replace("\n", " ").replace("\t", " ")):
        raise GroupRefused(422, "invalid_description", "Remove control characters from the description.")
    if len(text) > MAX_DESCRIPTION:
        raise GroupRefused(422, "invalid_description",
                           f"Use a description of at most {MAX_DESCRIPTION} characters.")
    return text or None


def clean_emails(raw: Iterable[str] | None) -> list[str]:
    out: list[str] = []
    for value in raw or ():
        email = normalize(str(value))
        if not email or len(email) > 320 or not _EMAIL.match(email):
            raise GroupRefused(422, "invalid_email", f"{str(value)[:80]} isn't an email address.")
        if email not in out:
            out.append(email)
    if len(out) > MAX_EMAILS:
        raise GroupRefused(422, "too_many", f"Add at most {MAX_EMAILS} people at a time.")
    return out


class GroupService:
    """Group management for organization administrators. Cheap to construct.
    Authority comes from the access store (`AccessService.require_org_admin`),
    never from the caller."""

    def __init__(self, hub_dir: Path):
        self.hub_dir = Path(hub_dir)

    # ---- plumbing -------------------------------------------------------------

    @property
    def store(self):
        from .access import store_for

        try:
            return store_for(self.hub_dir)
        except Exception:  # noqa: BLE001 — fail closed
            log.exception("groups: store unavailable")
            raise GroupRefused(503, "store_unavailable", "Access data is unavailable. Try again shortly.")

    def require_org_admin(self, actor) -> None:
        from .access.service import AccessService, Denied

        try:
            AccessService(self.hub_dir).require_org_admin(
                actor, "Only organization administrators can manage groups.")
        except Denied as exc:
            raise GroupRefused(exc.status, exc.code, exc.message)

    def _call(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except GroupNotFound as exc:
            raise GroupRefused(404, "not_found", str(exc) or "This group doesn't exist.")
        except GroupNameTaken as exc:
            raise GroupRefused(409, "name_taken", str(exc))
        except GroupError as exc:
            raise GroupRefused(422, "invalid_group", str(exc))
        except GroupRefused:
            raise
        except Exception:  # noqa: BLE001 — fail closed
            log.exception("groups: store error")
            raise GroupRefused(503, "store_unavailable", "Access data is unavailable. Try again shortly.")

    def _hub_names(self) -> dict[str, str]:
        from . import deployment

        try:
            return {h["key"]: h.get("name") or h["key"] for h in deployment.hubs(self.hub_dir)}
        except Exception:  # noqa: BLE001 — names are presentation only
            log.warning("groups: deployment unreadable while naming agents")
            return {}

    # ---- reads ----------------------------------------------------------------

    def list(self, actor) -> list[dict]:
        self.require_org_admin(actor)
        return [self._summary(g) for g in self._call(self.store.list_groups)]

    def get(self, actor, group_id: str) -> dict:
        self.require_org_admin(actor)
        return self._detail(group_id)

    def _detail(self, group_id: str) -> dict:
        gid = normalize(group_id)
        if group_id_of(GROUP_PREFIX + gid) is None:
            raise GroupRefused(404, "not_found", "This group doesn't exist.")
        gs = self.store
        group = self._call(gs.group, gid)
        if group is None:
            raise GroupRefused(404, "not_found", "This group doesn't exist.")
        out = self._summary(group)
        accounts = self._accounts([m["email"] for m in group["members"]])
        members = []
        for m in group["members"]:
            email = m["email"]
            try:
                identity = gs.identity(email) or {}
                blocked = gs.is_suspended(email)
            except Exception:  # noqa: BLE001
                raise GroupRefused(503, "store_unavailable", "Access data is unavailable. Try again shortly.")
            account = accounts.get(email)
            members.append({
                "email": email,
                "display": (account or {}).get("name") or identity.get("display") or None,
                "account": (account or {}).get("status") or ("none" if accounts is not None else None),
                "blocked": blocked,
                "added_at": m.get("added_at"),
                "added_by": m.get("added_by"),
            })
        out["members"] = members
        names = self._hub_names()
        by_hub: dict[str, list[str]] = {}
        for hub, perm in group["grants"]:
            by_hub.setdefault(hub, []).append(perm)
        out["access"] = [{"hub": hub, "hub_name": names.get(hub, hub), "permissions": sorted(perms)}
                         for hub, perms in sorted(by_hub.items())]
        return out

    @staticmethod
    def _summary(g: dict) -> dict:
        return {
            "id": g["id"],
            "subject": GROUP_PREFIX + g["id"],
            "name": g["name"],
            "description": g.get("description") or "",
            "source": g.get("source") or "console",
            "created_by": g.get("created_by"),
            "created_at": g.get("created_at"),
            "updated_at": g.get("updated_at"),
            "member_count": g.get("member_count", 0),
            "grant_count": g.get("grant_count", len(g.get("grants") or [])),
        }

    def _accounts(self, emails: list[str]) -> dict[str, dict] | None:
        """Hubzoid accounts for these emails: {email: {"name", "status"}}.
        None when the account table can't be read (legacy or not yet created)."""
        if not emails:
            return {}
        from sqlalchemy import bindparam, text

        try:
            with self.store.engine.connect() as conn:
                rows = conn.execute(text(
                    "SELECT email, name, status FROM hz_users WHERE lower(email) IN :emails"
                ).bindparams(bindparam("emails", expanding=True)), {"emails": emails}).fetchall()
        except Exception:  # noqa: BLE001 — presentation only
            log.debug("groups: account table unreadable", exc_info=True)
            return None
        return {normalize(e): {"name": n, "status": s} for e, n, s in rows}

    def groups_of(self, email: str) -> list[dict]:
        return self._call(self.store.groups_for, email)

    # ---- writes ---------------------------------------------------------------

    def _refuse_blocked(self, emails: list[str]) -> None:
        """A blocked person (or one whose account is unavailable) is not given
        access through a group, as they are not given it directly."""
        gs = self.store
        try:
            blocked = [e for e in emails if gs.is_suspended(e)]
        except Exception:  # noqa: BLE001 — fail closed
            raise GroupRefused(503, "store_unavailable", "Access data is unavailable. Try again shortly.")
        if blocked:
            who = ", ".join(blocked[:3]) + ("…" if len(blocked) > 3 else "")
            raise GroupRefused(409, "blocked",
                               f"Blocked people can't be added to a group: {who}. Nothing was changed.")

    def create(self, actor, *, name, description=None, emails=()) -> dict:
        self.require_org_admin(actor)
        name = clean_name(name)
        description = clean_description(description)
        members = clean_emails(emails)
        self._refuse_blocked(members)
        group = self._call(self.store.create_group, name, actor=actor.subject,
                           description=description, emails=members, surface=actor.surface)
        return self._detail(group["id"])

    def update(self, actor, group_id: str, *, name=None, description=UNSET) -> dict:
        self.require_org_admin(actor)
        kwargs = {}
        if name is not None:
            kwargs["name"] = clean_name(name)
        if description is not UNSET:
            kwargs["description"] = clean_description(description)
        if not kwargs:
            return self._detail(group_id)
        self._call(self.store.update_group, group_id, actor=actor.subject,
                   surface=actor.surface, **kwargs)
        return self._detail(group_id)

    def delete(self, actor, group_id: str) -> dict:
        self.require_org_admin(actor)
        result = self._call(self.store.delete_group, group_id, actor=actor.subject,
                            surface=actor.surface)
        self._project_visibility()
        return result

    def add_members(self, actor, group_id: str, emails) -> dict:
        self.require_org_admin(actor)
        members = clean_emails(emails)
        if not members:
            raise GroupRefused(422, "invalid_email", "Add at least one email address.")
        self._refuse_blocked(members)
        added = self._call(self.store.add_group_members, group_id, members,
                           actor=actor.subject, surface=actor.surface)
        self._project_visibility()
        out = self._detail(group_id)
        out["added"] = added
        return out

    def remove_member(self, actor, group_id: str, email: str) -> bool:
        self.require_org_admin(actor)
        email = normalize(email)
        if not email:
            raise GroupRefused(422, "invalid_email", "Choose a member to remove.")
        removed = self._call(self.store.remove_group_member, group_id, email,
                             actor=actor.subject, surface=actor.surface)
        if not removed:
            raise GroupRefused(404, "not_member", f"{email} isn't in this group.")
        self._project_visibility()
        return True

    def _project_visibility(self) -> None:
        """A legacy deployment mirrors agent visibility into the chat app; a
        membership change moves it like a grant does. Best effort."""
        try:
            from .access.service import AccessService

            AccessService(self.hub_dir)._project_visibility()  # noqa: SLF001
        except Exception:  # noqa: BLE001 — the periodic sync is the recovery path
            log.debug("groups: visibility sync skipped", exc_info=True)


def names_for(hub_dir, email: str | None) -> set[str]:
    """The normalized names of the groups `email` belongs to, for
    ``access.effective_groups`` in the web app mode. Empty for a blocked person
    and on any error (fail closed: a lookup that goes wrong denies)."""
    email = normalize(email or "")
    if not email or hub_dir is None:
        return set()
    try:
        from .access import store_for

        gs = store_for(Path(hub_dir))
        if gs.is_suspended(email):
            return set()
        return {normalize(g["name"]) for g in gs.groups_for(email) if normalize(g["name"])}
    except Exception:  # noqa: BLE001
        log.warning("groups: membership lookup failed; denying")
        return set()


def groups_of(hub_dir, email: str | None) -> list[dict]:
    """[{"id", "name"}] for `email`, or [] (fail closed)."""
    email = normalize(email or "")
    if not email:
        return []
    try:
        from .access import store_for

        return store_for(Path(hub_dir)).groups_for(email)
    except Exception:  # noqa: BLE001
        log.warning("groups: membership lookup failed")
        return []
