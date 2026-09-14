# Hubzoid access management. MIT licensed like the rest of the repository.
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
plus the restricted-function stems (`prod_in`, `nurturing_write`, ...).

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
ORG = "*"                      # the org domain: a grant here applies to every hub
EVERYONE = "*"                 # the wildcard subject: everyone who can log in
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

    def remove_filtered_policy(self, sec, ptype, field_index, *field_values) -> None:  # pragma: no cover
        pass


class LastAdminError(Exception):
    """Raised when a write would remove the final org admin."""


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
        self._rev = self._read_revision()

    # ---- reads ---------------------------------------------------------------

    def _read_revision(self) -> int:
        with self._engine.connect() as conn:
            row = conn.execute(text("SELECT rev FROM hz_policy_revision WHERE id=1")).fetchone()
        return int(row[0]) if row else 0

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
        self._refresh_if_stale()
        with self._lock:
            return bool(self._enforcer.enforce(subject, normalize(hub), normalize(action)))

    def permissions_for(self, subject: str, hub: str) -> set[str]:
        """Every permission `subject` effectively holds in `hub` (direct +
        org-wide + wildcard-subject). Used by the portal and denied-UX."""
        subject = normalize(subject)
        hub = normalize(hub)
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
        row = conn.execute(text("SELECT v FROM hz_meta WHERE k=:k"), {"k": key}).fetchone()
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

    def is_authoritative(self) -> bool:
        """True once Casbin is the authority for this deployment (set at fresh
        bootstrap or at migration cutover). Until then callers use legacy groups,
        so existing un-migrated hubs are untouched."""
        with self._engine.connect() as conn:
            return self._meta_get(conn, "casbin_authoritative") == "1"

    def set_authoritative(self, flag: bool = True) -> None:
        with self._engine.begin() as conn:
            self._meta_set(conn, "casbin_authoritative", "1" if flag else "0")

    def bootstrap(self, admin_subjects: Iterable[str] = (), *,
                  authoritative: bool = False) -> None:
        """First-boot bootstrap (idempotent): grant org `manage_access` to the
        given admins once, so no deployment — fresh or migrated — can lock itself
        out of the portal. `authoritative=True` also makes Casbin the authority
        (use for fresh installs with no legacy access to migrate)."""
        with self._engine.begin() as conn:
            if self._meta_get(conn, "bootstrapped") == "1":
                if authoritative:
                    self._meta_set(conn, "casbin_authoritative", "1")
                return
            for subj in admin_subjects:
                subj = (subj or "").strip()
                if subj:
                    self._insert_grant(conn, subj, ORG, MANAGE_ACCESS)
            self._meta_set(conn, "bootstrapped", "1")
            if authoritative:
                self._meta_set(conn, "casbin_authoritative", "1")
            self._bump_revision(conn)
        self._refresh_if_stale()

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

    # ---- writes (the grant_service; every write is one transaction) ----------

    def grant(self, subject: str, hub: str, permission: str) -> None:
        """Grant one permission. Granting any tool permission auto-grants
        `use_hub` in the same hub (the implication rule), so a grantee can always
        open a hub they have any permission in. Idempotent."""
        subject = normalize(subject)
        hub = normalize(hub)
        permission = normalize(permission)
        if not subject or not permission:
            raise ValueError("subject and permission are required")
        rows = [(subject, hub, permission)]
        # Implication: any hub-scoped permission implies use_hub (except in the
        # org domain, where use_hub is meaningless).
        if hub != ORG and permission not in (USE_HUB,):
            rows.append((subject, hub, USE_HUB))
        with self._engine.begin() as conn:
            for s, h, p in rows:
                self._insert_grant(conn, s, h, p)
            self._bump_revision(conn)
        self._refresh_if_stale()

    def revoke(self, subject: str, hub: str, permission: str) -> None:
        """Revoke one permission. Revoking `use_hub` cascades: it removes every
        permission the subject has in that hub (you can't hold a tool in a hub
        you can't enter). Refuses to remove the last org admin (race-safe)."""
        subject = normalize(subject)
        hub = normalize(hub)
        permission = normalize(permission)
        removes_admin = (hub == ORG and permission == MANAGE_ACCESS)
        with self._engine.begin() as conn:
            if removes_admin:
                # Serialize concurrent admin revokes: FOR UPDATE on Postgres; on
                # SQLite the write below takes the DB write lock. Then a
                # check-BEFORE and a check-AFTER-delete both in the txn, so two
                # revokes can never both pass and empty the admin set.
                admins = self._org_admins_locked(conn)
                if subject in admins and len(admins) <= 1:
                    raise LastAdminError(
                        "cannot remove the last org admin; grant another first"
                    )
            if hub != ORG and permission == USE_HUB:
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
            self._bump_revision(conn)
        self._refresh_if_stale()

    def revoke_all(self, subject: str) -> None:
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
            self._bump_revision(conn)
        self._refresh_if_stale()

    def grant_many(self, grants: Iterable[tuple[str, str, str]]) -> None:
        """Apply many (subject, hub, permission) grants in one transaction — the
        CSV-import / migration path. Applies the same use_hub implication."""
        expanded: list[tuple[str, str, str]] = []
        for subject, hub, permission in grants:
            subject = normalize(subject)
            hub = normalize(hub)
            permission = normalize(permission)
            if not subject or not permission:
                continue
            expanded.append((subject, hub, permission))
            if hub != ORG and permission != USE_HUB:
                expanded.append((subject, hub, USE_HUB))
        with self._engine.begin() as conn:
            for s, h, p in expanded:
                self._insert_grant(conn, s, h, p)
            self._bump_revision(conn)
        self._refresh_if_stale()

    # ---- internals -----------------------------------------------------------

    def _insert_grant(self, conn, subject: str, hub: str, permission: str) -> None:
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

    def _bump_revision(self, conn) -> None:
        conn.execute(text("UPDATE hz_policy_revision SET rev = rev + 1 WHERE id=1"))

    # ---- identities (subject rows) ------------------------------------------

    def upsert_identity(self, *, email: str | None = None, owui_id: str | None = None,
                        phone: str | None = None, display: str | None = None,
                        pending: bool = False) -> str:
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
                text("SELECT subject FROM hz_identities WHERE subject=:s"),
                {"s": subject},
            ).fetchone()
            fields = {
                "s": subject, "e": email_n, "o": (owui_id or None),
                "p": (phone or None), "d": (display or None),
                "pend": 1 if pending else 0, "t": time.time(),
            }
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

    # ---- attributes (per hub, subject) --------------------------------------

    def set_attr(self, hub: str, subject: str, key: str, value: str) -> None:
        """Set a per-(hub, subject) attribute (e.g. center). Casbin never reads
        these; tools do, for in-tool data scoping."""
        hub = normalize(hub)
        subject = normalize(subject)
        with self._engine.begin() as conn:
            dialect = conn.engine.dialect.name
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
