# Hubzoid access management. MIT licensed like the rest of the repository.
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
    grants: list[tuple[str, str, str]] = field(default_factory=list)      # (subject, hub, perm)
    attrs: list[tuple[str, str, str, str]] = field(default_factory=list)  # (hub, subject, k, v)
    conflicts: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

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
    if isinstance(roster, _FunctionRoster) and getattr(roster, "_groups_for_email", None):
        raise MigrationBlocked(
            "this hub uses a function-backed roster with groups_for_email(); its "
            "grants are computed live and are not statically enumerable. Export an "
            "explicit direct-grant snapshot (identity/access.csv) before migrating."
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
    seen: set[str] = set()
    with open(path, newline="") as f:
        for row in _csv.DictReader(f):
            email = (row.get("email") or "").strip().lower()
            if not email:
                continue
            if email in seen:
                plan.conflicts.append(f"duplicate email in access.csv: {email}")
            seen.add(email)
            groups = [g for g in (row.get("groups") or "").replace(",", ";").split(";") if g.strip()]
            for g in groups:
                plan.add_grant(email, hub_name, g)
            center = (row.get("center") or "").strip()
            if center:
                plan.attrs.append((hub_name, email, "center", center))
    return plan


# --- Open WebUI 0.11 source -------------------------------------------------

def _owui_public(access_control) -> bool:
    """OWUI 0.11: access_control == null (or missing) means public signed-in
    ('user/*'); an empty grants dict is NOT public (admin-only)."""
    return access_control is None


def plan_from_owui(engine: Engine, hub_name: str, *, model_id: str | None = None,
                   plan: MigrationPlan | None = None) -> MigrationPlan:
    """Read OWUI 0.11 group memberships + a model's access grants into the plan.

    Refuses an unknown schema (missing user/group/model tables) rather than
    guessing. `model_id` selects which model = this hub; if None, and there is a
    single model, that one is used."""
    plan = plan or MigrationPlan()
    hub_name = normalize(hub_name)
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    required = {"user", "group", "model"}
    if not required.issubset(tables):
        raise MigrationBlocked(
            f"unrecognized Open WebUI schema (need tables {sorted(required)}, "
            f"found {sorted(tables & required)}); refusing to guess"
        )

    with engine.connect() as conn:
        users = {
            r[0]: (r[1] or "").strip().lower()
            for r in conn.execute(text("SELECT id, email FROM user")).fetchall()
        }
        # groups: id -> (name, [user_ids])
        groups: dict[str, tuple[str, list[str]]] = {}
        for gid, name, uids in conn.execute(
            text("SELECT id, name, user_ids FROM \"group\"")
        ).fetchall():
            try:
                members = json.loads(uids) if uids else []
            except Exception:  # noqa: BLE001
                members = []
            groups[gid] = (name or "", members)

        # models: pick the hub's model
        model_rows = conn.execute(
            text("SELECT id, access_control FROM model")
        ).fetchall()

    model_by_id = {mid: ac for mid, ac in model_rows}
    if model_id is not None:
        if model_id not in model_by_id:
            # A missing model must NOT be silently treated as public (that would
            # grant wildcard use_hub). Refuse.
            raise MigrationBlocked(
                f"OWUI model_id {model_id!r} not found; refusing (a missing model "
                "must not become public)"
            )
        chosen = {model_id: model_by_id[model_id]}
    elif len(model_by_id) == 1:
        chosen = model_by_id
    else:
        chosen = model_by_id  # migrate all models found

    for mid, ac_raw in chosen.items():
        try:
            ac = json.loads(ac_raw) if isinstance(ac_raw, str) else ac_raw
        except Exception:  # noqa: BLE001
            ac = ac_raw
        if _owui_public(ac):
            plan.add_grant(EVERYONE, hub_name, USE_HUB)
            continue
        # access_control = {"read": {"group_ids": [...], "user_ids": [...]}, "write": {...}}
        for section in ("read", "write"):
            block = (ac or {}).get(section, {}) if isinstance(ac, dict) else {}
            for uid in block.get("user_ids", []) or []:
                email = users.get(uid)
                if email:
                    plan.add_grant(email, hub_name, USE_HUB)
            for gid in block.get("group_ids", []) or []:
                name, members = groups.get(gid, ("", []))
                for uid in members:
                    email = users.get(uid)
                    if email:
                        plan.add_grant(email, hub_name, USE_HUB)
    return plan


# --- apply / diff -----------------------------------------------------------

def apply(store: GrantStore, plan: MigrationPlan, *, authoritative: bool = True) -> None:
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
    store.grant_many(plan.grants)
    for hub, subject, k, v in plan.attrs:
        store.set_attr(hub, subject, k, v)
    # Record an identity row per real grantee (email subjects), so email/owui_id
    # mappings exist for later hardening. The wildcard subject is not a person.
    for subject in {s for s, _h, _p in plan.grants}:
        if subject and subject != EVERYONE and "@" in subject:
            store.upsert_identity(email=subject)
    if authoritative:
        store.set_authoritative(True)


def diff(store: GrantStore, plan: MigrationPlan) -> dict:
    """Full static diff between the plan and the live store. Zero missing/extra
    is the cutover gate. Compares (subject, hub, permission) including the
    implied use_hub."""
    want: set[tuple[str, str, str]] = set()
    for subject, hub, perm in plan.grants:
        want.add((subject, hub, perm))
        if hub != "*" and perm != USE_HUB:
            want.add((subject, hub, USE_HUB))
    have = set(store.list_grants())
    return {
        "missing": sorted(want - have),   # planned but not in store
        "extra": sorted(have - want),     # in store but not planned
    }
