# Hubzoid access management. Apache-2.0 licensed like the rest of the repository.
"""One-time migration of legacy access into the Casbin store (direct grants).

Two legacy sources, both flattened to direct `(subject, hub, permission)` grants:
  * `identity/access.csv` — `phone,email,groups[,center]`. The email is the
    subject; each group is a permission in this hub; `center` becomes a per-hub
    attribute.
  * Open WebUI 0.11 — group memberships + a model's access grants. A model open
    to `user/*` (public signed-in) or a standalone bypass maps to a **wildcard
    `use_hub`**; group grants flatten to each member's direct grants.

Groups never survive as grant-bearing entities (that would be the second data
model). A hub whose roster is **function-backed** with live `groups_for_email`
authz cannot be statically enumerated, so migration **hard-fails** (preflight)
and asks for an explicit `access.csv` snapshot first.

Nothing is authoritative until cutover: `apply(..., authoritative=True)` writes
the marker that makes Casbin the authority (and turns on the OWUI-UI lock).
"""

from __future__ import annotations

import csv as _csv
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from .identity import normalize
from .store import EVERYONE, USE_HUB, GrantStore

log = logging.getLogger("hubzoid.access.migrate")


class MigrationBlocked(Exception):
    """Preflight refused: the hub's access can't be safely enumerated."""


@dataclass
class MigrationPlan:
    grants: list[tuple[str, str, str]] = field(
        default_factory=list
    )  # (subject, hub, perm)
    attrs: list[tuple[str, str, str, str]] = field(
        default_factory=list
    )  # (hub, subject, k, v)
    conflicts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    expected: list[tuple[str, str, str, bool]] = field(default_factory=list)
    visibility_backup: dict | None = None
    identities: list[dict] = field(default_factory=list)

    def add_grant(self, subject: str, hub: str, perm: str) -> None:
        self.grants.append(((subject or "").strip(), normalize(hub), normalize(perm)))


# --- preflight --------------------------------------------------------------


def preflight(hub_dir) -> list[str]:
    """Refuse (raise) if the hub's roster is a function that computes authz live
    (`groups_for_email`), which cannot be statically enumerated. Returns
    warnings otherwise."""
    from .resolver import load_resolver
    from .resolver import _FunctionRoster  # type: ignore

    roster = load_resolver(Path(hub_dir))
    if isinstance(roster, _FunctionRoster):
        # A function roster computes identity/authz live (groups_for_email, or a
        # resolve() that may return groups) — not statically enumerable. Refuse
        # rather than risk dropping live permissions at cutover; the owner exports
        # an explicit direct-grant snapshot (identity/access.csv) first.
        raise MigrationBlocked(
            "this hub uses a function-backed roster (identity/access.py); its "
            "grants may be computed live and are not statically enumerable. Export "
            "an explicit direct-grant snapshot (identity/access.csv) before "
            "migrating (then remove access.py or keep it for identity only)."
        )
    return []


# --- CSV source -------------------------------------------------------------


def _find_csv(hub_dir: Path) -> Path | None:
    from .._fs import resolve_bucket

    ident = resolve_bucket(hub_dir, "identity")
    if ident is None:
        return None
    path = ident / "access.csv"
    return path if path.exists() else None


def plan_from_csv(hub_dir, hub_name: str | None = None) -> MigrationPlan:
    """Flatten `identity/access.csv` into a migration plan."""
    hub_dir = Path(hub_dir)
    hub_name = normalize(hub_name or hub_dir.name)
    preflight(hub_dir)
    plan = MigrationPlan()
    path = _find_csv(hub_dir)
    if path is None:
        plan.warnings.append("no identity/access.csv found")
        return plan
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = _csv.DictReader(f)
        reader.fieldnames = [name.strip().lower() for name in (reader.fieldnames or [])]
        for row in reader:
            email = (row.get("email") or "").strip().lower()
            if not email:
                continue
            groups = [
                g
                for g in (row.get("groups") or "").replace(",", ";").split(";")
                if g.strip()
            ]
            for g in groups:
                if normalize(g) in ("manage_access", "*"):
                    plan.conflicts.append(
                        f"reserved permission in legacy groups for {email}: {g}"
                    )
                    continue
                plan.add_grant(email, hub_name, g)
            center = (row.get("center") or "").strip()
            if center:
                previous = [
                    v
                    for h, s, k, v in plan.attrs
                    if h == hub_name and s == email and k == "center"
                ]
                if previous and center not in previous:
                    plan.conflicts.append(f"conflicting center values for {email}")
                elif not previous:
                    plan.attrs.append((hub_name, email, "center", center))
    return plan


def plan_standalone_public(hub_dir, plan: MigrationPlan) -> MigrationPlan:
    """Explicit legacy standalone bypass: signed-in entry was public.

    Tool decisions come from the actual legacy roster resolver, independently of
    the grant plan. Gateways must use model ACL evidence instead.
    """
    from ..deployment import permission_catalog, read
    from .resolver import load_resolver

    hub_dir = Path(hub_dir)
    if read(hub_dir):
        raise MigrationBlocked(
            "registered gateways require --from-owui, not --standalone-public"
        )
    roster = load_resolver(hub_dir)
    hub = normalize(hub_dir.name)
    subjects = {normalize(s) for s, _, _ in plan.grants} | {"__future_signed_in__"}
    permissions = {p["permission"] for p in permission_catalog(hub_dir)} - {
        "use_hub",
        "manage_access",
    }
    permissions.update(p for _, _, p in plan.grants)
    plan.add_grant(EVERYONE, hub, USE_HUB)
    for subject in sorted(subjects):
        plan.expected.append((subject, hub, USE_HUB, True))
        groups = set(roster.groups_for_email(subject)) if roster else set()
        for permission in sorted(permissions - {USE_HUB}):
            plan.expected.append((subject, hub, permission, permission in groups))
    return plan


# --- Open WebUI 0.11 source -------------------------------------------------


def _owui_public(access_control) -> bool:
    """OWUI 0.11: access_control == null (or missing) means public signed-in
    ('user/*'); an empty grants dict is NOT public (admin-only)."""
    return access_control is None


def plan_from_owui(
    engine: Engine,
    hub_name: str,
    *,
    model_id: str | None = None,
    plan: MigrationPlan | None = None,
    permissions=(),
    standalone_public: bool = False,
) -> MigrationPlan:
    """Read supported legacy or normalized OWUI schemas; never guess access.

    Preserve the intersection of model visibility and tool-group membership.
    The independent expected matrix includes denied users, not just grants.
    """
    plan = plan or MigrationPlan()
    hub_name = normalize(hub_name)
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    if not {"user", "group", "model"} <= tables:
        raise MigrationBlocked(
            "unrecognized Open WebUI schema: missing user/group/model"
        )

    def cols(table: str) -> set[str]:
        return {c["name"] for c in insp.get_columns(table)}

    modern_membership = "group_member" in tables
    modern = "access_grant" in tables
    if (not modern_membership and "user_ids" not in cols("group")) or (
        not standalone_public and not modern and "access_control" not in cols("model")
    ):
        raise MigrationBlocked("unrecognized Open WebUI membership/access schema")
    with engine.connect() as c:
        users = {
            r.id: dict(r) for r in c.execute(text('SELECT * FROM "user"')).mappings()
        }
        emails = {uid: normalize(u.get("email", "")) for uid, u in users.items()}
        plan.identities = [
            dict(email=u["email"], owui_id=uid, pending=u.get("role") == "pending")
            for uid, u in users.items()
            if u.get("email")
        ]
        pending_emails = {
            emails[uid] for uid, u in users.items() if u.get("role") == "pending"
        }
        groups = {
            r.id: dict(r) for r in c.execute(text('SELECT * FROM "group"')).mappings()
        }
        members = {gid: set() for gid in groups}
        if modern_membership:
            for gid, uid in c.execute(
                text("SELECT group_id, user_id FROM group_member")
            ):
                if gid in members:
                    members[gid].add(uid)
        else:
            for gid, g in groups.items():
                try:
                    members[gid] = set(json.loads(g.get("user_ids") or "[]"))
                except (ValueError, TypeError):
                    raise MigrationBlocked(f"invalid membership JSON for group {gid}")
        if standalone_public:
            if model_id:
                raise MigrationBlocked(
                    "standalone public bypass does not select a model ACL"
                )
            public = True
            allowed = set(users)
        else:
            models = {
                r.id: dict(r) for r in c.execute(text("SELECT * FROM model")).mappings()
            }
            if model_id is None and len(models) == 1:
                model_id = next(iter(models))
            if model_id not in models:
                raise MigrationBlocked("select an existing OWUI model with --model-id")
            model = models[model_id]
            if model.get("is_active") in (False, 0):
                raise MigrationBlocked(
                    "selected OWUI model is disabled; review its intended access before migration"
                )
            allowed = {uid for uid, u in users.items() if u.get("role") == "admin"}
            if model.get("user_id") in users:
                allowed.add(model["user_id"])
            public = False
            if modern:
                grants = c.execute(
                    text(
                        "SELECT principal_type, principal_id, permission FROM access_grant "
                        "WHERE resource_type='model' AND resource_id=:id"
                    ),
                    {"id": model_id},
                ).fetchall()
                plan.visibility_backup = dict(
                    model_id=model_id,
                    access_grants=[
                        dict(principal_type=t, principal_id=p, permission=a)
                        for t, p, a in grants
                    ],
                )
                for typ, principal, permission in grants:
                    if permission not in ("read", "write"):
                        continue
                    if principal == "*" and typ in ("user", "anyone"):
                        public = True
                    elif typ == "user":
                        allowed.add(principal)
                    elif typ == "group":
                        allowed.update(members.get(principal, set()))
            else:
                raw = model.get("access_control")
                try:
                    ac = json.loads(raw) if isinstance(raw, str) else raw
                except ValueError:
                    raise MigrationBlocked("invalid model access JSON")
                public = ac is None
                if ac is not None and not isinstance(ac, dict):
                    raise MigrationBlocked("invalid model access structure")
                original = []
                if public:
                    original.append(
                        dict(principal_type="user", principal_id="*", permission="read")
                    )
                for action in ("read", "write"):
                    block = (ac or {}).get(action, {})
                    original.extend(
                        dict(principal_type="user", principal_id=uid, permission=action)
                        for uid in block.get("user_ids", []) or []
                    )
                    original.extend(
                        dict(
                            principal_type="group", principal_id=gid, permission=action
                        )
                        for gid in block.get("group_ids", []) or []
                    )
                    allowed.update(block.get("user_ids", []) or [])
                    for gid in block.get("group_ids", []) or []:
                        allowed.update(members.get(gid, set()))
                plan.visibility_backup = dict(model_id=model_id, access_grants=original)
    tool_perms = {normalize(p) for p in permissions} - {USE_HUB, "manage_access"}
    # CSV group permissions and OWUI groups previously formed a union.
    old_csv = {(normalize(s), p) for s, h, p in plan.grants if h == hub_name}
    all_emails = set(emails.values()) | {s for s, _ in old_csv}
    entry = {emails[u] for u in allowed if u in emails}
    if public:
        entry |= all_emails
    plan.grants = [g for g in plan.grants if g[1] != hub_name]
    if public:
        plan.add_grant(EVERYONE, hub_name, USE_HUB)
    membership = {}
    for gid, ids in members.items():
        perm = normalize(groups[gid].get("name", ""))
        for uid in ids:
            if uid in emails:
                membership.setdefault(emails[uid], set()).add(perm)
    for email in sorted(all_emails | {"__future_signed_in__"}):
        may_enter = public or email in entry
        active = email not in pending_emails
        plan.expected.append((email, hub_name, USE_HUB, may_enter and active))
        if may_enter and not public:
            plan.add_grant(email, hub_name, USE_HUB)
        for perm in sorted(tool_perms):
            may_use = may_enter and (
                perm in membership.get(email, set()) or (email, perm) in old_csv
            )
            plan.expected.append((email, hub_name, perm, may_use and active))
            if may_use:
                plan.add_grant(email, hub_name, perm)
    return plan


def effective_diff(store: GrantStore, plan: MigrationPlan) -> list[dict]:
    return [
        dict(subject=s, hub=h, permission=p, before=allowed, after=store.can(s, h, p))
        for s, h, p, allowed in plan.expected
        if store.can(s, h, p) != allowed
    ]


# --- apply / diff -----------------------------------------------------------


def verify_effective(plan: MigrationPlan) -> list[dict]:
    """Compare legacy decisions with a clean candidate before changing live access."""
    if not plan.expected:
        return []
    from sqlalchemy import create_engine

    candidate_engine = create_engine("sqlite://")
    try:
        candidate = GrantStore(candidate_engine)
        candidate.grant_many(plan.grants)
        for identity in plan.identities:
            candidate.upsert_identity(**identity)
        return effective_diff(candidate, plan)
    finally:
        candidate_engine.dispose()


def apply(
    store: GrantStore, plan: MigrationPlan, *, authoritative: bool = True
) -> None:
    """Apply a plan in one pass, then (optionally) make Casbin authoritative —
    the cutover. Grants get the use_hub implication for free via grant_many.

    Refuses to cut over (make Casbin authoritative) on an empty or conflicted
    plan — that would lock everyone out of a hub that was open a moment ago."""
    if authoritative and not plan.grants:
        raise MigrationBlocked(
            "refusing to cut over with zero grants (empty plan) — this would lock "
            "everyone out. Provide access.csv / OWUI source, or use --apply only "
            "with a non-empty plan."
        )
    if authoritative and plan.conflicts:
        raise MigrationBlocked(
            f"refusing to cut over with unresolved conflicts: {plan.conflicts}"
        )
    mismatch = verify_effective(plan)
    if mismatch:
        raise MigrationBlocked(f"effective access would change: {mismatch[:10]}")
    hubs = {normalize(h) for _s, h, _p in plan.grants if h and normalize(h) != "*"}
    if not authoritative:
        # a plain (non-cutover) apply: just add the grants, no marker
        store.grant_many(plan.grants)
        for hub, subject, k, v in plan.attrs:
            store.set_attr(hub, subject, k, v)
        return
    # The cutover: one atomic, hub-scoped, replace-semantics transaction that
    # also sets the per-hub authority markers.
    try:
        store.apply_migration(
            plan.grants,
            plan.attrs,
            hubs,
            replace=True,
            authoritative=True,
            identities=plan.identities,
        )
    except ValueError as exc:
        raise MigrationBlocked(str(exc)) from exc


def diff(store: GrantStore, plan: MigrationPlan) -> dict:
    """Full static diff between the plan and the live store. Zero missing/extra
    is the cutover gate. Compares (subject, hub, permission) including the
    implied use_hub."""
    want: set[tuple[str, str, str]] = set()
    hubs: set[str] = set()
    for subject, hub, perm in plan.grants:
        subject, hub, perm = normalize(subject), normalize(hub), normalize(perm)
        want.add((subject, hub, perm))
        hubs.add(hub)
        if hub != "*" and perm != USE_HUB:
            want.add((subject, hub, USE_HUB))
    # Only compare within the migrated hubs — other hubs' rows on a shared DB are
    # not "extra".
    have = {(s, h, p) for s, h, p in store.list_grants() if h in hubs}
    return {
        "missing": sorted(want - have),  # planned but not in store
        "extra": sorted(have - want),  # in store but not planned (stale)
    }
