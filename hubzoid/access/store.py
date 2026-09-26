# Hubzoid access management. Apache-2.0 licensed like the rest of the repository.
"""The one access store: per-hub permissions on Casbin, direct grants only.

This is the single authority every surface consults through `can(subject, hub,
action)`. It replaces the old "permission in identity.groups" union with one
store (`hz_grants`), so there is exactly one place that answers "what can this
person do in this hub?".

Model (RBAC-with-domains, but direct grants — no role bundles):
  * a grant is a policy row  `p, subject, hub, permission`
  * subject `*`   = everyone who can log in (public `use_hub`)
  * hub `*`       = the reserved ORG domain (org-level `manage_access`)
  * permission `*`= all permissions in that scope (unused today, kept for the
                    matcher's symmetry)

Two permissions matter to every hub:
  * `use_hub`        — may open/use the hub at all (hub entry)
  * `manage_access`  — may grant/revoke within a scope (delegated admin)
plus the restricted-function stems (`crm_read`, `billing_write`, ...).

Writes go through `GrantStore` methods only (never straight SQL from callers),
so every write is one transaction that also bumps `hz_policy_revision`. `can()`
reloads the in-memory enforcer when the revision changes, which is how many
bridge processes over one shared DB stay fresh.

The surface gate (`policy.py`) still runs in FRONT of this: a grant is
necessary, not sufficient.
"""

from __future__ import annotations

import threading
from typing import Iterable

import casbin
from casbin.persist import Adapter
from sqlalchemy import text
from sqlalchemy.engine import Engine

from . import db_tables
from .identity import normalize

# Reserved scopes / permissions.
ORG = "*"  # the org domain: a grant here applies to every hub
EVERYONE = "*"  # the wildcard subject: everyone who can log in
USE_HUB = "use_hub"
MANAGE_ACCESS = "manage_access"

# Direct-grants model: a request (sub, dom, act) is allowed if any policy row
# matches, where subject/domain/permission each match exactly or via `*`.
_MODEL_CONF = """\
[request_definition]
r = sub, dom, act

[policy_definition]
p = sub, dom, act

[policy_effect]
e = some(where (p.eft == allow))

[matchers]
m = (r.sub == p.sub || p.sub == "*") && (r.dom == p.dom || p.dom == "*") && (r.act == p.act || p.act == "*")
"""


def _validate_grant(subject: str, hub: str, permission: str) -> None:
    if not subject or not permission:
        raise ValueError("subject and permission are required")
    if not hub:
        raise ValueError("hub is required")
    if hub == ORG and permission != MANAGE_ACCESS:
        raise ValueError("the organization domain only supports manage_access")
    if subject == EVERYONE and (permission != USE_HUB or hub == ORG):
        raise ValueError("public grants only support use_hub in a named hub")
    if permission == "*":
        # A '*' permission would match every action in the matcher, incl.
        # manage_access — never a grantable value.
        raise ValueError("the wildcard permission '*' is not grantable")


class _Adapter(Adapter):
    """Loads `hz_grants` into the Casbin model. Writes are done by GrantStore
    directly against the table (transactionally), so the save-side methods are
    no-ops; the enforcer is used read-only and reloaded after each write."""

    def __init__(self, engine: Engine):
        self._engine = engine

    def load_policy(self, model) -> None:
        with self._engine.connect() as conn:
            rows = conn.execute(
                text("SELECT subject, hub, permission FROM hz_grants")
            ).fetchall()
        for sub, dom, act in rows:
            model.add_policy("p", "p", [sub, dom, act])

    # Casbin's Adapter interface — unused because GrantStore owns writes.
    def save_policy(self, model) -> bool:  # pragma: no cover - never called
        return True

    def add_policy(self, sec, ptype, rule) -> None:  # pragma: no cover
        pass

    def remove_policy(self, sec, ptype, rule) -> None:  # pragma: no cover
        pass

    def remove_filtered_policy(
        self, sec, ptype, field_index, *field_values
    ) -> None:  # pragma: no cover
        pass


class LastAdminError(Exception):
    """Raised when a write would remove the final org admin."""


class RevisionConflict(Exception):
    """Raised when a guarded write finds the policy revision has moved since the
    caller loaded it (another admin edited access first)."""


class GrantStore:
    """The access store for one deployment's database (one hub, or the shared
    gateway DB). Cheap to construct; holds a Casbin enforcer kept fresh against
    `hz_policy_revision`."""

    def __init__(self, engine: Engine):
        self._engine = engine
        db_tables.ensure_access_tables(engine)
        model = casbin.Model()
        model.load_model_from_text(_MODEL_CONF)
        self._enforcer = casbin.Enforcer(model, _Adapter(engine))
        self._lock = threading.Lock()
        self._rev = (
            -1
        )  # first decision reloads: never pair old policy with a newer revision

    # ---- reads ---------------------------------------------------------------

    def _read_revision(self) -> int:
        with self._engine.connect() as conn:
            row = conn.execute(
                text("SELECT rev FROM hz_policy_revision WHERE id=1")
            ).fetchone()
        return int(row[0]) if row else 0

    def revision(self) -> int:
        """Current policy revision. Every grant/revoke bumps it in the same
        transaction, so callers can use it for optimistic concurrency."""
        return self._read_revision()

    def _read_revision_locked(self, conn) -> int:
        """Read the revision while holding the write lock, so a concurrent commit
        cannot slip in between the check and the writes that follow. On Postgres
        that's SELECT ... FOR UPDATE; on SQLite a no-op UPDATE takes the DB write
        lock before the read."""
        if conn.engine.dialect.name == "sqlite":
            conn.execute(text("UPDATE hz_policy_revision SET rev = rev WHERE id=1"))
            row = conn.execute(
                text("SELECT rev FROM hz_policy_revision WHERE id=1")
            ).fetchone()
        else:
            row = conn.execute(
                text("SELECT rev FROM hz_policy_revision WHERE id=1 FOR UPDATE")
            ).fetchone()
        return int(row[0]) if row else 0

    def access_snapshot(self) -> tuple[int, list[tuple[str, str, str]]]:
        """A consistent (revision, every grant) pair, so the portal can hand the
        editor a revision that matches exactly the rows it shows. Reading the two
        separately risks new rows under an old revision (or the reverse), which
        would defeat the concurrency guard.

        Transaction isolation across two statements is not guaranteed the same way
        on SQLite (rollback-journal vs WAL) and PostgreSQL (READ COMMITTED by
        default), so we do a read-verify loop: read revision, read grants, read
        revision again; if a write landed in between the revision moved and we
        retry. This is correct on any engine and isolation level."""
        self._refresh_if_stale()
        last_rev = 0
        for _ in range(8):
            with self._engine.connect() as conn:
                rev1 = int(
                    conn.execute(
                        text("SELECT rev FROM hz_policy_revision WHERE id=1")
                    ).scalar()
                    or 0
                )
                rows = conn.execute(
                    text("SELECT subject, hub, permission FROM hz_grants")
                ).fetchall()
                rev2 = int(
                    conn.execute(
                        text("SELECT rev FROM hz_policy_revision WHERE id=1")
                    ).scalar()
                    or 0
                )
            if rev1 == rev2:
                return rev1, [(s, h, p) for (s, h, p) in rows]
            last_rev = rev2
        # Extremely unlikely: writes on every attempt. Fall back to a locked read
        # so the pair is at least internally consistent under the write lock.
        with self._engine.begin() as conn:
            rev = self._read_revision_locked(conn)
            rows = conn.execute(
                text("SELECT subject, hub, permission FROM hz_grants")
            ).fetchall()
        return rev or last_rev, [(s, h, p) for (s, h, p) in rows]

    def _grant_in_txn(self, conn, subject, hub, permission, actor,
                      surface=None, request_id=None) -> None:
        rows = [(subject, hub, permission)]
        if hub != ORG and permission != USE_HUB:
            rows.append((subject, hub, USE_HUB))
        for s, h, p in rows:
            self._insert_grant(conn, s, h, p)
            self._audit(conn, actor, "grant", s, h, p, surface, request_id)

    def _revoke_in_txn(self, conn, subject, hub, permission, actor,
                       surface=None, request_id=None) -> None:
        removes_admin = hub == ORG and permission == MANAGE_ACCESS
        if removes_admin:
            admins = self._org_admins_locked(conn)
            if subject in admins and len(admins) <= 1:
                raise LastAdminError(
                    "cannot remove the last org admin; grant another first"
                )
        if hub != ORG and permission == USE_HUB:
            for (removed,) in conn.execute(
                text("SELECT permission FROM hz_grants WHERE subject=:s AND hub=:h"),
                {"s": subject, "h": hub},
            ):
                if removed != USE_HUB:
                    self._audit(conn, actor, "revoke", subject, hub, removed,
                                surface, request_id)
            conn.execute(
                text("DELETE FROM hz_grants WHERE subject=:s AND hub=:h"),
                {"s": subject, "h": hub},
            )
        else:
            conn.execute(
                text(
                    "DELETE FROM hz_grants WHERE subject=:s AND hub=:h AND permission=:p"
                ),
                {"s": subject, "h": hub, "p": permission},
            )
        if removes_admin and not self._org_admins(conn):
            raise LastAdminError(
                "cannot remove the last org admin; grant another first"
            )
        self._audit(conn, actor, "revoke", subject, hub, permission, surface, request_id)

    def apply_changes(
        self,
        subject: str,
        hub: str,
        operations: "Iterable[tuple[str, str]]",
        *,
        expected_revision: int | None = None,
        actor: str | None = None,
        surface: str | None = None,
        request_id: str | None = None,
    ) -> int:
        """Apply one subject's whole change set for a hub in a SINGLE transaction,
        guarded by `expected_revision`. `operations` are (action, permission) with
        action 'grant'|'revoke'. Mirrors grant()/revoke() semantics (use_hub
        implication and cascade, last-admin protection). The revision is checked
        under the write lock and the writes commit together, so a concurrent edit
        cannot slip between the check and the apply. Returns the new revision;
        raises RevisionConflict if the store moved since `expected_revision`."""
        subject = normalize(subject)
        hub = normalize(hub)
        ops = [(a, normalize(p)) for a, p in operations]
        for _action, p in ops:
            _validate_grant(subject, hub, p)
        with self._engine.begin() as conn:
            current = self._read_revision_locked(conn)
            if expected_revision is not None and current != expected_revision:
                raise RevisionConflict(
                    "Access changed since you loaded it — someone else edited it. "
                    "Reload and review the current access before saving."
                )
            for action, p in ops:
                if action == "revoke":
                    self._revoke_in_txn(conn, subject, hub, p, actor, surface, request_id)
                else:
                    self._grant_in_txn(conn, subject, hub, p, actor, surface, request_id)
            self._bump_revision(conn)
        self._refresh_if_stale()
        return self.revision()

    def _refresh_if_stale(self) -> None:
        current = self._read_revision()
        if current != self._rev:
            with self._lock:
                self._enforcer.load_policy()
                self._rev = current

    def can(self, subject: str, hub: str, action: str) -> bool:
        """The authority. True if `subject` holds `action` in `hub` (or org-wide,
        or via the public wildcard). Reloads if another process wrote."""
        subject = normalize(subject)
        if not subject:
            return False
        if self.is_suspended(subject):
            return False
        self._refresh_if_stale()
        with self._lock:
            return bool(
                self._enforcer.enforce(subject, normalize(hub), normalize(action))
            )

    def permissions_for(self, subject: str, hub: str) -> set[str]:
        """Every permission `subject` effectively holds in `hub` (direct +
        org-wide + wildcard-subject). Used by the portal and denied-UX."""
        subject = normalize(subject)
        hub = normalize(hub)
        if self.is_suspended(subject):
            return set()
        self._refresh_if_stale()
        out: set[str] = set()
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT subject, hub, permission FROM hz_grants "
                    "WHERE (subject=:s OR subject='*') AND (hub=:h OR hub='*')"
                ),
                {"s": subject, "h": hub},
            ).fetchall()
        for _sub, _hub, perm in rows:
            out.add(perm)
        return out

    def hubs_for(self, subject: str) -> set[str]:
        """The hubs `subject` may open (`use_hub`), for the visibility mirror.
        Excludes the org domain; a wildcard `use_hub` grant means "all hubs" and
        is returned as the sentinel '*'."""
        subject = normalize(subject)
        self._refresh_if_stale()
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT hub FROM hz_grants "
                    "WHERE (subject=:s OR subject='*') AND permission=:u"
                ),
                {"s": subject, "u": USE_HUB},
            ).fetchall()
        return {h for (h,) in rows}

    def list_grants(self, hub: str | None = None) -> list[tuple[str, str, str]]:
        """All (subject, hub, permission) rows, optionally for one hub."""
        self._refresh_if_stale()
        q = "SELECT subject, hub, permission FROM hz_grants"
        params: dict = {}
        if hub is not None:
            q += " WHERE hub=:h"
            params["h"] = normalize(hub)
        q += " ORDER BY subject, hub, permission"
        with self._engine.connect() as conn:
            return [tuple(r) for r in conn.execute(text(q), params).fetchall()]

    # ---- authority marker + bootstrap ---------------------------------------

    def _meta_get(self, conn, key: str) -> str | None:
        row = conn.execute(
            text("SELECT v FROM hz_meta WHERE k=:k"), {"k": key}
        ).fetchone()
        return row[0] if row else None

    def _meta_set(self, conn, key: str, value: str) -> None:
        dialect = conn.engine.dialect.name
        if dialect == "sqlite":
            conn.execute(
                text(
                    "INSERT INTO hz_meta(k, v) VALUES(:k, :v) "
                    "ON CONFLICT (k) DO UPDATE SET v=excluded.v"
                ),
                {"k": key, "v": value},
            )
        else:
            conn.execute(
                text(
                    "INSERT INTO hz_meta(k, v) VALUES(:k, :v) "
                    "ON CONFLICT (k) DO UPDATE SET v=excluded.v"
                ),
                {"k": key, "v": value},
            )

    def is_authoritative(self, hub: str | None = None) -> bool:
        """True once Casbin is the authority for this hub. A **per-hub** marker
        (`casbin_authoritative:<hub>`) is checked first, then the deployment-wide
        one — so on a shared gateway DB, migrating hub A does NOT flip hubs B–N
        (which stay legacy until their own cutover). Un-migrated hubs are
        untouched. Cached and refreshed on a policy_revision change so a
        per-request check is a cheap in-memory read, not a DB hit."""
        self._refresh_if_stale()
        hub = normalize(hub) if hub else None
        with self._engine.connect() as conn:
            if hub:
                marker = self._meta_get(conn, f"casbin_authoritative:{hub}")
                if marker is not None:
                    return marker == "1"
            return self._meta_get(conn, "casbin_authoritative") == "1"

    def any_authoritative(self) -> bool:
        """True if the deployment has migrated any hub (global marker or any
        per-hub marker). Used to decide the gateway-wide OWUI access-UI lock."""
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT 1 FROM hz_meta WHERE "
                    "(k = 'casbin_authoritative' OR k LIKE 'casbin_authoritative:%') "
                    "AND v = '1' LIMIT 1"
                )
            ).fetchone()
        return bool(row)

    def set_authoritative(self, flag: bool = True, *, hub: str | None = None) -> None:
        key = (
            f"casbin_authoritative:{normalize(hub)}" if hub else "casbin_authoritative"
        )
        with self._engine.begin() as conn:
            self._meta_set(conn, key, "1" if flag else "0")
            self._bump_revision(conn)

    def bootstrap(
        self,
        admin_subjects: Iterable[str] = (),
        *,
        authoritative: bool = False,
        hub: str | None = None,
    ) -> None:
        """First-boot bootstrap (idempotent): grant org `manage_access` to the
        given admins once, so no deployment — fresh or migrated — can lock itself
        out of the portal. `authoritative=True` also makes Casbin the authority
        (use for fresh installs with no legacy access to migrate)."""
        marker = (
            f"casbin_authoritative:{normalize(hub)}" if hub else "casbin_authoritative"
        )
        with self._engine.begin() as conn:
            if self._meta_get(conn, "bootstrapped") == "1":
                if authoritative:
                    self._meta_set(conn, marker, "1")
                    self._audit(conn, "bootstrap", "activate", None, hub or ORG, None)
                    self._bump_revision(conn)
                return
            granted = 0
            for subj in admin_subjects:
                subj = normalize(subj)
                if subj:
                    self._insert_grant(conn, subj, ORG, MANAGE_ACCESS)
                    self._ensure_identity(conn, subj)
                    self._audit(conn, "bootstrap", "grant", subj, ORG, MANAGE_ACCESS)
                    granted += 1
            # Only consume the one-shot marker once a real admin exists — an
            # empty bootstrap() must NOT block a later legitimate admin list.
            if granted:
                self._meta_set(conn, "bootstrapped", "1")
            # Refuse to make Casbin authoritative with no org admin at all — that
            # is an unrecoverable web lockout (nobody can pass the portal gate).
            if authoritative:
                if not (granted or self._org_admins(conn)):
                    raise LastAdminError(
                        "refusing authoritative bootstrap with no org admin — "
                        "pass at least one --admin"
                    )
                self._meta_set(conn, marker, "1")
                self._audit(conn, "bootstrap", "activate", None, ORG, None)
            self._bump_revision(conn)
        self._refresh_if_stale()

    def provision_owner(self, subject: str, hub: str, *, fresh: bool = False) -> bool:
        """Provision a configured, verified owner once per hub, never on every login.

        Caller verifies the account and matches it to operator configuration.
        The marker survives grant revocation and prevents login restoring access.
        Existing hubs retain their authority mode until explicitly migrated.
        """
        subject, hub = normalize(subject), normalize(hub)
        _validate_grant(subject, hub, USE_HUB)
        marker = f"initial_owner:{hub}"
        with self._engine.begin() as conn:
            self._read_revision_locked(conn)
            if self._meta_get(conn, marker):
                return False
            if self._meta_get(conn, "suspended:" + subject) == "1":
                return False
            # Organization setup is once per store, not once per hub. Adding
            # another hub to a gateway must not restore a revoked org role or
            # replace the administrators chosen by an earlier bootstrap.
            if not self._meta_get(conn, "bootstrapped"):
                self._grant_in_txn(conn, subject, ORG, MANAGE_ACCESS, "owner-setup")
            self._grant_in_txn(conn, subject, hub, USE_HUB, "owner-setup")
            self._ensure_identity(conn, subject)
            self._meta_set(conn, marker, subject)
            self._meta_set(conn, "bootstrapped", "1")
            if fresh:
                self._meta_set(conn, f"casbin_authoritative:{hub}", "1")
            self._bump_revision(conn)
        self._refresh_if_stale()
        return True

    def _org_admins(self, conn) -> set[str]:
        rows = conn.execute(
            text("SELECT subject FROM hz_grants WHERE hub=:o AND permission=:m"),
            {"o": ORG, "m": MANAGE_ACCESS},
        ).fetchall()
        return {s for (s,) in rows}

    def _org_admins_locked(self, conn) -> set[str]:
        """Like _org_admins but takes a row lock on Postgres (FOR UPDATE) so
        concurrent admin revokes serialize. SQLite has no row lock; its
        single-writer transaction plus the check-after-delete covers it."""
        sql = "SELECT subject FROM hz_grants WHERE hub=:o AND permission=:m"
        if conn.engine.dialect.name != "sqlite":
            sql += " FOR UPDATE"
        rows = conn.execute(text(sql), {"o": ORG, "m": MANAGE_ACCESS}).fetchall()
        return {s for (s,) in rows}

    def _audit(self, conn, actor, action, subject, hub, permission,
               surface=None, request_id=None) -> None:
        import time

        conn.execute(
            text(
                "INSERT INTO hz_access_audit "
                "(ts, actor, action, subject, hub, permission, surface, request_id) "
                "VALUES (:t, :a, :ac, :s, :h, :p, :su, :r)"
            ),
            {
                "t": time.time(),
                "a": actor,
                "ac": action,
                "s": subject,
                "h": hub,
                "p": permission,
                "su": surface,
                "r": request_id,
            },
        )

    def write_audit(self, conn, actor, action, *, subject=None, hub=None,
                    permission=None, surface=None, request_id=None) -> None:
        """Record one access-audit row inside the caller's transaction, so a
        state change and its audit row commit together. Never pass secrets."""
        self._audit(conn, actor, action, subject, hub, permission, surface, request_id)

    def audit_event(self, actor, action, *, subject=None, hub=None,
                    permission=None, surface=None, request_id=None) -> None:
        """Record one access-audit row in its own transaction."""
        with self._engine.begin() as conn:
            self._audit(conn, actor, action, subject, hub, permission, surface, request_id)

    @property
    def engine(self) -> Engine:
        """The operational database engine this store writes to."""
        return self._engine

    def publish_workflows(
        self, hub: str, workflows: Iterable[tuple[str, str | None, str | None]]
    ) -> None:
        """A bridge publishes its hub's workflow catalog to the shared store, so
        the org portal can list every hub's workflows. Replaces this hub's rows."""
        import time

        hub = normalize(hub)
        rows = list(workflows)
        with self._engine.begin() as conn:
            conn.execute(text("DELETE FROM hz_workflows WHERE hub=:h"), {"h": hub})
            for name, schedule, tz in rows:
                conn.execute(
                    text(
                        "INSERT INTO hz_workflows (hub, name, schedule, timezone, updated) "
                        "VALUES (:h, :n, :s, :t, :u)"
                    ),
                    {"h": hub, "n": name, "s": schedule, "t": tz, "u": time.time()},
                )

    def list_workflows(self, hubs: "Iterable[str] | None" = None) -> list[dict]:
        """The workflow catalog, optionally restricted to a set of hubs (for a
        hub admin's scoped view)."""
        q = "SELECT hub, name, schedule, timezone FROM hz_workflows"
        params: dict = {}
        allow = None
        if hubs is not None:
            allow = {normalize(h) for h in hubs}
        with self._engine.connect() as conn:
            rows = conn.execute(text(q + " ORDER BY hub, name"), params).fetchall()
        keys = ("hub", "name", "schedule", "timezone")
        out = [dict(zip(keys, r)) for r in rows]
        return [w for w in out if allow is None or w["hub"] in allow]

    def read_access_audit(
        self,
        limit: int = 100,
        *,
        hubs=None,
        subject=None,
        actor=None,
        action=None,
        since=None,
        until=None,
        offset=0,
        request_id=None,
    ) -> list[dict]:
        """Recent access CHANGE events (grant/revoke), newest first. Filters are
        applied in SQL, so pagination is over the filtered set, not the page."""
        clauses, params = [], {"n": limit, "offset": offset}
        if hubs is not None:
            if not hubs:
                return []
            names = []
            for i, h in enumerate(hubs):
                params[f"h{i}"] = h
                names.append(f":h{i}")
            clauses.append("hub IN (" + ",".join(names) + ")")
        if subject:
            clauses.append("subject = :subject")
            params["subject"] = normalize(subject)
        if actor:
            clauses.append("actor = :actor")
            params["actor"] = normalize(actor)
        if action:
            clauses.append("action = :action")
            params["action"] = action
        if since is not None:
            clauses.append("ts >= :since")
            params["since"] = float(since)
        if until is not None:
            clauses.append("ts <= :until")
            params["until"] = float(until)
        if request_id:
            clauses.append("request_id = :request_id")
            params["request_id"] = request_id
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT ts, actor, action, subject, hub, permission, surface, request_id "
                    "FROM hz_access_audit"
                    + where
                    + " ORDER BY ts DESC LIMIT :n OFFSET :offset"
                ),
                params,
            ).fetchall()
        keys = ("ts", "actor", "action", "subject", "hub", "permission", "surface", "request_id")
        return [dict(zip(keys, r)) for r in rows]

    # ---- writes (the grant_service; every write is one transaction) ----------

    def grant(
        self, subject: str, hub: str, permission: str, *, actor: str | None = None
    ) -> None:
        """Grant one permission. Granting any tool permission auto-grants
        `use_hub` in the same hub (the implication rule), so a grantee can always
        open a hub they have any permission in. Idempotent."""
        subject = normalize(subject)
        hub = normalize(hub)
        permission = normalize(permission)
        _validate_grant(subject, hub, permission)
        with self._engine.begin() as conn:
            self._grant_in_txn(conn, subject, hub, permission, actor)
            self._bump_revision(conn)
        self._refresh_if_stale()

    def revoke(
        self, subject: str, hub: str, permission: str, *, actor: str | None = None
    ) -> None:
        """Revoke one permission. Revoking `use_hub` cascades: it removes every
        permission the subject has in that hub (you can't hold a tool in a hub
        you can't enter). Refuses to remove the last org admin (race-safe)."""
        subject = normalize(subject)
        hub = normalize(hub)
        permission = normalize(permission)
        with self._engine.begin() as conn:
            self._revoke_in_txn(conn, subject, hub, permission, actor)
            self._bump_revision(conn)
        self._refresh_if_stale()

    def revoke_all(self, subject: str, *, actor: str | None = None,
                   surface: str | None = None, request_id: str | None = None) -> None:
        """Remove every grant for a subject across all hubs (portal 'revoke all').
        Refuses if it would remove the last org admin (race-safe)."""
        subject = normalize(subject)
        with self._engine.begin() as conn:
            admins = self._org_admins_locked(conn)
            if subject in admins and len(admins) <= 1:
                raise LastAdminError(
                    "cannot remove the last org admin; grant another first"
                )
            conn.execute(text("DELETE FROM hz_grants WHERE subject=:s"), {"s": subject})
            if not self._org_admins(conn):
                raise LastAdminError(
                    "cannot remove the last org admin; grant another first"
                )
            self._audit(conn, actor, "revoke_all", subject, ORG, None, surface, request_id)
            self._bump_revision(conn)
        self._refresh_if_stale()

    def apply_migration(
        self,
        grants: Iterable[tuple[str, str, str]],
        attrs: Iterable[tuple[str, str, str, str]],
        hubs: Iterable[str],
        *,
        replace: bool = True,
        authoritative: bool = True,
        actor: str = "migration",
        identities: Iterable[dict] = (),
    ) -> None:
        """The migration cutover, in ONE transaction: (optionally) replace the
        target hubs' grants, insert the plan (with use_hub implication), set
        attributes + identity rows, set the PER-HUB authority markers, and bump
        the revision. Atomic — a crash rolls the whole thing back, and replace
        semantics mean no stale grant survives cutover."""
        import time

        hubs = [normalize(h) for h in hubs if h and normalize(h) != ORG]
        attrs = list(attrs)
        if any(normalize(h) not in hubs for h, _, _, _ in attrs):
            raise ValueError("migration attributes outside target hubs")
        expanded: list[tuple[str, str, str]] = []
        emails: set[str] = set()
        for subject, hub, permission in grants:
            subject = normalize(subject)
            hub = normalize(hub)
            permission = normalize(permission)
            _validate_grant(subject, hub, permission)
            expanded.append((subject, hub, permission))
            if hub != ORG and permission != USE_HUB:
                expanded.append((subject, hub, USE_HUB))
            if hub not in hubs:
                raise ValueError("migration grant outside target hubs")
            if subject != EVERYONE and "@" in subject:
                emails.add(subject)
        with self._engine.begin() as conn:
            self._lock_hubs(conn, hubs)
            if replace:
                for h in hubs:
                    self._audit(conn, actor, "replace_hub_grants", None, h, None)
                    conn.execute(text("DELETE FROM hz_grants WHERE hub=:h"), {"h": h})
            for s, h, p in expanded:
                self._insert_grant(conn, s, h, p)
                self._audit(conn, actor, "grant", s, h, p)
            for hub, subject, k, v in attrs:
                conn.execute(
                    text(
                        "INSERT INTO hz_identity_attrs (hub, subject, k, v) "
                        "VALUES (:h, :s, :k, :v) "
                        "ON CONFLICT (hub, subject, k) DO UPDATE SET v=excluded.v"
                    ),
                    {"h": normalize(hub), "s": normalize(subject), "k": k, "v": v},
                )
            for email in emails:
                conn.execute(
                    text(
                        "INSERT INTO hz_identities (subject, email, created) "
                        "VALUES (:s, :s, :t) ON CONFLICT (subject) DO NOTHING"
                    ),
                    {"s": email, "t": time.time()},
                )
            for identity in identities:
                subject = normalize(identity["email"])
                self._ensure_identity(conn, subject)
                previous = conn.execute(
                    text("SELECT owui_id FROM hz_identities WHERE subject=:s"),
                    {"s": subject},
                ).scalar()
                if previous and previous != identity["owui_id"]:
                    raise ValueError(
                        "OWUI account changed for "
                        + subject
                        + "; refresh and review accounts before migration"
                    )
                conn.execute(
                    text(
                        "UPDATE hz_identities SET owui_id=:o, pending=:p WHERE subject=:s"
                    ),
                    {
                        "o": identity["owui_id"],
                        "p": int(identity.get("pending", False)),
                        "s": subject,
                    },
                )
                self._meta_set(
                    conn,
                    "account_unavailable:" + subject,
                    "1" if identity.get("pending") else "0",
                )
            if authoritative:
                for h in hubs:
                    self._meta_set(conn, f"casbin_authoritative:{h}", "1")
            self._bump_revision(conn)
        self._refresh_if_stale()

    def grant_many(
        self, grants: Iterable[tuple[str, str, str]], *, actor: str = "bulk-import"
    ) -> None:
        """Apply many (subject, hub, permission) grants in one transaction — the
        CSV-import / migration path. Applies the same use_hub implication."""
        expanded: list[tuple[str, str, str]] = []
        for subject, hub, permission in grants:
            subject = normalize(subject)
            hub = normalize(hub)
            permission = normalize(permission)
            _validate_grant(subject, hub, permission)
            expanded.append((subject, hub, permission))
            if hub != ORG and permission != USE_HUB:
                expanded.append((subject, hub, USE_HUB))
        with self._engine.begin() as conn:
            for s, h, p in expanded:
                self._insert_grant(conn, s, h, p)
                self._audit(conn, actor, "grant", s, h, p)
            self._bump_revision(conn)
        self._refresh_if_stale()

    # ---- internals -----------------------------------------------------------

    def _insert_grant(self, conn, subject: str, hub: str, permission: str) -> None:
        self._ensure_identity(conn, subject)
        dialect = conn.engine.dialect.name
        if dialect == "sqlite":
            conn.execute(
                text(
                    "INSERT OR IGNORE INTO hz_grants(subject, hub, permission) "
                    "VALUES (:s, :h, :p)"
                ),
                {"s": subject, "h": hub, "p": permission},
            )
        else:
            conn.execute(
                text(
                    "INSERT INTO hz_grants(subject, hub, permission) VALUES (:s, :h, :p) "
                    "ON CONFLICT (subject, hub, permission) DO NOTHING"
                ),
                {"s": subject, "h": hub, "p": permission},
            )

    @staticmethod
    def _lock_hubs(conn, hubs) -> None:
        # PostgreSQL DELETE does not lock an empty hub's key space. Serialize
        # whole-hub replacement so two concurrent cutovers cannot merge plans.
        if conn.engine.dialect.name == "postgresql":
            for hub in sorted(set(hubs)):
                conn.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": "hubzoid-access:" + hub},
                )

    def _bump_revision(self, conn) -> None:
        conn.execute(text("UPDATE hz_policy_revision SET rev = rev + 1 WHERE id=1"))

    # ---- identities (subject rows) ------------------------------------------

    def upsert_identity(
        self,
        *,
        email: str | None = None,
        owui_id: str | None = None,
        phone: str | None = None,
        display: str | None = None,
        pending: bool = False,
    ) -> str:
        """Record/refresh a grantee's identity row and return its subject id.

        The subject is the stable key everything grants to; today it is the
        normalized email (the interim used by migration), with owui_id/phone as
        lookup columns filled at login/resolve for future email-recycling
        hardening. Idempotent."""
        import time

        email_n = normalize(email) if email else None
        subject = email_n or normalize(owui_id) or normalize(phone)
        if not subject:
            raise ValueError("need at least one of email/owui_id/phone")
        with self._engine.begin() as conn:
            row = conn.execute(
                text("SELECT subject, owui_id FROM hz_identities WHERE subject=:s"),
                {"s": subject},
            ).fetchone()
            fields = {
                "s": subject,
                "e": email_n,
                "o": (owui_id or None),
                "p": (phone or None),
                "d": (display or None),
                "pend": 1 if pending else 0,
                "t": time.time(),
            }
            if row and owui_id and row[1] and row[1] != owui_id:
                # A new account reusing an email must not inherit the old owner's
                # direct grants, including administrator rights.
                conn.execute(
                    text("DELETE FROM hz_grants WHERE subject=:s"), {"s": subject}
                )
                self._meta_set(conn, "suspended:" + subject, "1")
                self._audit(
                    conn, "owui-identity", "account_replaced", subject, ORG, None
                )
                self._bump_revision(conn)
            if owui_id:
                self._meta_set(
                    conn, "account_unavailable:" + subject, "1" if pending else "0"
                )
            if row:
                conn.execute(
                    text(
                        "UPDATE hz_identities SET "
                        "email=COALESCE(:e, email), owui_id=COALESCE(:o, owui_id), "
                        "phone=COALESCE(:p, phone), display=COALESCE(:d, display), "
                        "pending=:pend WHERE subject=:s"
                    ),
                    fields,
                )
            else:
                conn.execute(
                    text(
                        "INSERT INTO hz_identities (subject, email, owui_id, phone, "
                        "display, pending, created) VALUES (:s, :e, :o, :p, :d, :pend, :t)"
                    ),
                    fields,
                )
        return subject

    def identity(self, subject: str) -> dict | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT subject, email, owui_id, phone, display, pending "
                    "FROM hz_identities WHERE subject=:s"
                ),
                {"s": normalize(subject)},
            ).fetchone()
        if not row:
            return None
        keys = ("subject", "email", "owui_id", "phone", "display", "pending")
        return dict(zip(keys, row))

    def bind_new_account(
        self,
        subject: str,
        *,
        owui_id: str,
        display: str | None,
        grants: "dict[str, Iterable[tuple[str, str]]]",
        actor: str,
        expected_revision: int | None = None,
        replace: bool = False,
        surface: str | None = None,
        request_id: str | None = None,
    ) -> int:
        """Bind a just-created chat account to its subject and apply its initial
        grants in ONE transaction, audited as `account_create` plus each grant.

        `grants` maps hub -> (action, permission) operations. An identity already
        bound to a different chat account is the account-replacement case: it is
        refused unless `replace` (an organization administrator re-creating it),
        which removes the old account's grants first, audited as
        `account_replaced`. A blocked subject is refused. Raises RevisionConflict
        when `expected_revision` no longer matches, so the caller's authority
        check and this write describe the same policy state."""
        import time

        subject = normalize(subject)
        planned = {
            normalize(h): [(a, normalize(p)) for a, p in ops] for h, ops in grants.items()
        }
        for hub, ops in planned.items():
            if hub == ORG:
                raise ValueError("initial account grants are per agent")
            for _action, p in ops:
                _validate_grant(subject, hub, p)
        with self._engine.begin() as conn:
            current = self._read_revision_locked(conn)
            if expected_revision is not None and current != expected_revision:
                raise RevisionConflict(
                    "Access changed while the account was being created. Review and try again."
                )
            if self._meta_get(conn, "suspended:" + subject) == "1":
                raise ValueError(
                    "This person is blocked. Reactivate them under People first."
                )
            row = conn.execute(
                text("SELECT owui_id FROM hz_identities WHERE subject=:s"),
                {"s": subject},
            ).fetchone()
            previous = row[0] if row else None
            if previous and previous != owui_id:
                if not replace:
                    raise ValueError(
                        "This email belonged to an earlier chat account. An organization "
                        "administrator must re-create it."
                    )
                conn.execute(text("DELETE FROM hz_grants WHERE subject=:s"), {"s": subject})
                self._audit(conn, actor, "account_replaced", subject, ORG, None,
                            surface, request_id)
            fields = {"s": subject, "o": owui_id, "d": display or None, "t": time.time()}
            if row:
                conn.execute(
                    text(
                        "UPDATE hz_identities SET email=:s, owui_id=:o, "
                        "display=COALESCE(:d, display), pending=0 WHERE subject=:s"
                    ),
                    fields,
                )
            else:
                conn.execute(
                    text(
                        "INSERT INTO hz_identities (subject, email, owui_id, display, pending, "
                        "created) VALUES (:s, :s, :o, :d, 0, :t)"
                    ),
                    fields,
                )
            self._meta_set(conn, "account_unavailable:" + subject, "0")
            self._audit(conn, actor, "account_create", subject, ORG, None, surface, request_id)
            for hub, ops in planned.items():
                for action, p in ops:
                    if action == "revoke":
                        self._revoke_in_txn(conn, subject, hub, p, actor, surface, request_id)
                    else:
                        self._grant_in_txn(conn, subject, hub, p, actor, surface, request_id)
            self._bump_revision(conn)
        self._refresh_if_stale()
        return self.revision()

    def mark_account_removed(self, subject: str, *, actor: str,
                             surface: str | None = None,
                             request_id: str | None = None) -> None:
        """Record that the subject's chat account was deleted: it is unavailable
        until an account is bound to it again. Grants are removed separately
        (`revoke_all`) before the account itself is deleted."""
        subject = normalize(subject)
        with self._engine.begin() as conn:
            self._meta_set(conn, "account_unavailable:" + subject, "1")
            self._audit(conn, actor, "account_delete", subject, ORG, None, surface, request_id)
            self._bump_revision(conn)
        self._refresh_if_stale()

    # ---- attributes (per hub, subject) --------------------------------------

    def set_attr(self, hub: str, subject: str, key: str, value: str) -> None:
        """Set a per-(hub, subject) attribute (e.g. center). Casbin never reads
        these; tools do, for in-tool data scoping."""
        hub = normalize(hub)
        subject = normalize(subject)
        with self._engine.begin() as conn:
            sql = (
                "INSERT INTO hz_identity_attrs (hub, subject, k, v) "
                "VALUES (:h, :s, :k, :v) "
                "ON CONFLICT (hub, subject, k) DO UPDATE SET v=excluded.v"
            )
            conn.execute(text(sql), {"h": hub, "s": subject, "k": key, "v": value})

    def get_attr(self, hub: str, subject: str, key: str, default=None):
        hub = normalize(hub)
        subject = normalize(subject)
        with self._engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT v FROM hz_identity_attrs WHERE hub=:h AND subject=:s AND k=:k"
                ),
                {"h": hub, "s": subject, "k": key},
            ).fetchone()
        return row[0] if row else default

    def attrs_for(self, hub: str, subject: str) -> dict:
        hub = normalize(hub)
        subject = normalize(subject)
        with self._engine.connect() as conn:
            rows = conn.execute(
                text("SELECT k, v FROM hz_identity_attrs WHERE hub=:h AND subject=:s"),
                {"h": hub, "s": subject},
            ).fetchall()
        return {k: v for k, v in rows}

    def _ensure_identity(self, conn, subject):
        import time

        if subject != EVERYONE:
            conn.execute(
                text(
                    "INSERT INTO hz_identities (subject, email, pending, created) "
                    "VALUES (:s, :e, 1, :t) ON CONFLICT (subject) DO NOTHING"
                ),
                {
                    "s": subject,
                    "e": subject if "@" in subject else None,
                    "t": time.time(),
                },
            )

    def is_suspended(self, subject: str) -> bool:
        with self._engine.connect() as conn:
            return any(
                self._meta_get(conn, prefix + normalize(subject)) == "1"
                for prefix in ("suspended:", "account_unavailable:")
            )

    def suspend(self, subject: str, *, actor: str, suspended=True,
                surface: str | None = None, request_id: str | None = None) -> None:
        subject = normalize(subject)
        if not subject or subject == EVERYONE:
            raise ValueError("a person or service is required")
        with self._engine.begin() as conn:
            admins = self._org_admins_locked(conn)
            if suspended and subject in admins and len(admins) <= 1:
                raise LastAdminError("cannot suspend the last org admin")
            if suspended:
                conn.execute(
                    text("DELETE FROM hz_grants WHERE subject=:s"), {"s": subject}
                )
            self._meta_set(conn, "suspended:" + subject, "1" if suspended else "0")
            self._audit(
                conn,
                actor,
                "suspend" if suspended else "reactivate",
                subject,
                ORG,
                None,
                surface,
                request_id,
            )
            self._bump_revision(conn)
        self._refresh_if_stale()

    def reconcile_accounts(self, people: list[dict]) -> None:
        """A successful complete OWUI directory read updates account availability.

        Keep pre-granted signup emails; only previously bound, missing accounts
        are unavailable. A failed/partial directory fetch must never call this.
        """
        observed = {normalize(p["email"]) for p in people}
        for person in people:
            self.upsert_identity(
                email=person["email"],
                owui_id=person["id"],
                display=person.get("name"),
                pending=person.get("role") == "pending",
            )
        with self._engine.begin() as conn:
            rows = conn.execute(
                text("SELECT subject FROM hz_identities WHERE owui_id IS NOT NULL")
            ).fetchall()
            for (subject,) in rows:
                if subject not in observed:
                    key = "account_unavailable:" + subject
                    if self._meta_get(conn, key) != "1":
                        self._meta_set(conn, key, "1")
                        self._audit(
                            conn,
                            "owui-identity",
                            "account_unavailable",
                            subject,
                            ORG,
                            None,
                        )

    def identities(self) -> list[dict]:
        with self._engine.connect() as conn:
            return [
                dict(r)
                for r in conn.execute(
                    text(
                        "SELECT subject, email, owui_id, display, pending FROM hz_identities ORDER BY subject"
                    )
                ).mappings()
            ]

    def snapshot(self, hubs: list[str]) -> dict:
        hubs = sorted({normalize(h) for h in hubs})
        with self._engine.connect() as c:
            attrs = [
                list(r)
                for r in c.execute(
                    text("SELECT hub, subject, k, v FROM hz_identity_attrs")
                )
                if r[0] in hubs
            ]
        return dict(
            version=1,
            hubs=hubs,
            grants=[g for g in self.list_grants() if g[1] in hubs],
            attrs=attrs,
            authority={h: self.is_authoritative(h) for h in hubs},
        )

    def restore(self, snapshot: dict, *, actor: str) -> None:
        if not isinstance(snapshot, dict) or snapshot.get("version") != 1:
            raise ValueError("unsupported access backup")
        try:
            hubs = set(snapshot["hubs"])
            if not hubs or any(
                not isinstance(h, str) or not h or h != normalize(h) or h == ORG
                for h in hubs
            ):
                raise ValueError("invalid backup scope")
            if set(snapshot["authority"]) != hubs or any(
                type(v) is not bool for v in snapshot["authority"].values()
            ):
                raise ValueError("invalid backup authority")
            for sub, h, p in snapshot["grants"]:
                if (
                    any(
                        not isinstance(v, str) or v != normalize(v) for v in (sub, h, p)
                    )
                    or h not in hubs
                ):
                    raise ValueError("invalid backup grant scope")
                _validate_grant(sub, h, p)
            for h, sub, k, v in snapshot["attrs"]:
                if h not in hubs or not all(isinstance(x, str) for x in (h, sub, k, v)):
                    raise ValueError("invalid backup attribute")
        except (KeyError, TypeError) as exc:
            raise ValueError("invalid access backup structure") from exc
        with self._engine.begin() as c:
            self._lock_hubs(c, hubs)
            for h in hubs:
                c.execute(text("DELETE FROM hz_grants WHERE hub=:h"), {"h": h})
                c.execute(text("DELETE FROM hz_identity_attrs WHERE hub=:h"), {"h": h})
                self._meta_set(
                    c,
                    "casbin_authoritative:" + h,
                    "1" if snapshot["authority"][h] else "0",
                )
                self._audit(c, actor, "rollback", None, h, None)
            for sub, h, p in snapshot["grants"]:
                self._insert_grant(c, sub, h, p)
                self._audit(c, actor, "restore_grant", sub, h, p)
            for h, sub, k, v in snapshot["attrs"]:
                if h not in hubs:
                    raise ValueError("invalid attribute scope")
                c.execute(
                    text(
                        "INSERT INTO hz_identity_attrs (hub,subject,k,v) VALUES (:h,:s,:k,:v)"
                    ),
                    dict(h=h, s=sub, k=k, v=v),
                )
            self._bump_revision(c)
        self._refresh_if_stale()

    def runtime_health(self, hub: str) -> dict:
        import json

        with self._engine.connect() as c:
            raw = self._meta_get(c, "workflow_health:" + normalize(hub))
        return json.loads(raw) if raw else {}

    def set_runtime_health(self, hub: str, **values) -> None:
        import json

        old = self.runtime_health(hub)
        old.update(values)
        with self._engine.begin() as c:
            self._meta_set(c, "workflow_health:" + normalize(hub), json.dumps(old))

    def paused_workflows(self, hub: str) -> set[str]:
        """Scheduled work the operator paused in `hub` (`md:<task>` for markdown
        tasks, the function name for code workflows). Dispatchers skip these."""
        return set(self.metadata("workflow_paused:" + normalize(hub), []) or [])

    def set_workflow_paused(self, hub: str, name: str, paused: bool, *, actor: str) -> None:
        """Pause or resume one workflow's schedule, audited in the same transaction."""
        import json

        key = "workflow_paused:" + normalize(hub)
        with self._engine.begin() as c:
            raw = self._meta_get(c, key)
            names = set(json.loads(raw) if raw else [])
            names = (names | {name}) if paused else (names - {name})
            self._meta_set(c, key, json.dumps(sorted(names)))
            self._audit(c, actor, "workflow_pause" if paused else "workflow_resume",
                        None, normalize(hub), name)

    def audit_run_control(self, hub: str, action: str, target: str, *, actor: str) -> None:
        """Record an operator's run control (e.g. a cancel) in the access audit."""
        with self._engine.begin() as c:
            self._audit(c, actor, action, None, normalize(hub), target)

    HOLD_KEY = "maintenance:hold"

    def schedule_hold(self) -> dict | None:
        """An active hold on new scheduled runs for every hub on this store
        (set by `hubzoid backup`), or None. A hold expires on its own, so a
        backup that dies cannot stop the schedule for good."""
        import time

        hold = self.metadata(self.HOLD_KEY)
        return hold if hold and float(hold.get("until", 0)) > time.time() else None

    def set_schedule_hold(self, reason: str, seconds: float, *, actor: str) -> None:
        import time

        self.set_metadata(self.HOLD_KEY, {"reason": reason, "by": actor,
                                          "until": time.time() + seconds})

    def clear_schedule_hold(self) -> None:
        with self._engine.begin() as c:
            c.execute(text("DELETE FROM hz_meta WHERE k=:k"), {"k": self.HOLD_KEY})

    def metadata(self, key: str, default=None):
        import json

        with self._engine.connect() as c:
            value = self._meta_get(c, key)
        return json.loads(value) if value else default

    def set_metadata(self, key: str, value) -> None:
        import json

        with self._engine.begin() as c:
            self._meta_set(c, key, json.dumps(value))
