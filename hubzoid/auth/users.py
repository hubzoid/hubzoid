"""Hubzoid accounts: the people who can sign in (``hz_users``).

One row per person in the deployment's shared operational store, so every
bridge of a gateway sees the same accounts. ``id`` keeps the id a person had
in Open WebUI when they were migrated, which keeps ``hz_identities.owui_id``,
workflow ``run_as`` bindings and report shares valid. New people get a random
UUID, the same shape.

Rules held here:
  * Emails are stored normalized (trimmed, lowercase) and are unique.
  * ``role`` is ``admin`` or ``user``; ``status`` is ``active`` or ``pending``.
    Suspension is not a status: it stays in the access store (``hz_meta``).
  * Password hashes never leave this module except through ``password_hash``
    (used to verify a sign-in). Public views never include them.
  * Changing a password, the role or the status to pending ends the person's
    sessions. Deleting a person also deletes their sessions, external sign-in
    identities and one-time links (and their personal connection tokens, when
    ``hubzoid.connectors.tokens`` is installed).
  * ``updated_at`` moves on every change to the account row (not on sign-in
    activity, ``last_login_at``). A session only starts if it hasn't moved
    since the credential was read (``sessions.create_session``).
  * External identities migrated from Open WebUI, which recorded the provider
    but not the issuer, carry the placeholder issuer
    ``openwebui-migrated:<provider>`` until that provider next signs the person
    in (``adopt_migrated_identity``).

SQL is SQLAlchemy Core over the tables in ``auth.schema``, portable between
SQLite and PostgreSQL. Database errors propagate: callers deny (fail closed).
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Mapping

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from . import LOCAL_OWNER_EMAIL, passwords
from .schema import engine_for, identities, links, ready, sessions, users

log = logging.getLogger("hubzoid.auth")

ROLES = ("admin", "user")
MIGRATED_ISSUER_PREFIX = "openwebui-migrated:"
STATUSES = ("active", "pending")
# How an account came to exist (hz_users.source).
SOURCES = ("local", "admin", "signup", "bootstrap", "migrated", "oidc")
LOCAL_OWNER_NAME = "Local owner"

_EMAIL = re.compile(r"^[^\s@]+@[^\s@]+$")
# What people may register or be invited with (a dotted domain), as the Console checks.
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")


class AccountExists(Exception):
    """Another account already uses this email (or id)."""


class InvalidAccount(ValueError):
    """A value outside the account rules. ``message`` is safe to show."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def normalize_email(email: str | None) -> str:
    return (email or "").strip().lower()


def _public(row) -> dict | None:
    if row is None:
        return None
    m = row._mapping
    return {
        "id": m["id"],
        "email": m["email"],
        "name": m["name"] or "",
        "role": m["role"],
        "status": m["status"],
        "password_enabled": bool(m["password_enabled"]),
        "has_password": bool(m["password_hash"]),
        "source": m["source"],
        "created_at": m["created_at"],
        "updated_at": m["updated_at"],
        "last_login_at": m["last_login_at"],
    }


def is_local_address(email: str | None) -> bool:
    """``admin@localhost`` and other ``localhost`` addresses: the local owner's
    kind, which no password or external provider signs in to."""
    domain = normalize_email(email).rpartition("@")[2]
    return domain == "localhost" or domain.endswith(".localhost")


def sign_in_of(user: Mapping) -> str:
    """How the person signs in, for people reading it: "google" (an external
    provider only), otherwise "password"."""
    return "password" if user.get("password_enabled") else "google"


class UserStore:
    """Accounts in one operational database. Cheap; holds no per-person state."""

    def __init__(self, engine: Engine):
        self.engine = ready(engine)

    # ---- reads ------------------------------------------------------------------

    def get(self, user_id: str | None) -> dict | None:
        if not user_id:
            return None
        with self.engine.connect() as conn:
            row = conn.execute(sa.select(users).where(users.c.id == str(user_id))).first()
        return _public(row)

    def find_by_email(self, email: str | None) -> dict | None:
        email = normalize_email(email)
        if not email:
            return None
        with self.engine.connect() as conn:
            row = conn.execute(sa.select(users).where(users.c.email == email)).first()
        return _public(row)

    def list(self) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(sa.select(users).order_by(users.c.email)).fetchall()
        return [_public(r) for r in rows]

    def count(self, *, exclude_local: bool = False) -> int:
        q = sa.select(sa.func.count()).select_from(users)
        if exclude_local:
            q = q.where(users.c.source != "local")
        with self.engine.connect() as conn:
            return int(conn.execute(q).scalar() or 0)

    def admins(self, *, exclude_local: bool = False) -> list[dict]:
        """Active administrators. ``exclude_local`` leaves out the local owner,
        which cannot sign in once sign-in is on."""
        q = sa.select(users).where(users.c.role == "admin", users.c.status == "active")
        if exclude_local:
            q = q.where(users.c.source != "local")
        with self.engine.connect() as conn:
            rows = conn.execute(q.order_by(users.c.email)).fetchall()
        return [_public(r) for r in rows]

    def credentials(self, email: str | None) -> tuple[dict | None, str | None]:
        """(account, password hash) for a sign-in, in one query whatever the
        outcome, so an unknown email costs the same database work. The hash is
        None without a password or when password sign-in is off."""
        email = normalize_email(email)
        with self.engine.connect() as conn:
            row = conn.execute(sa.select(users).where(users.c.email == email)).first()
        if row is None:
            return None, None
        m = row._mapping
        return _public(row), (m["password_hash"] or None) if m["password_enabled"] else None

    def password_hash(self, user_id: str) -> str | None:
        """The stored hash, for verifying a sign-in only. None when the person
        has no password or signs in with an external provider only."""
        with self.engine.connect() as conn:
            row = conn.execute(
                sa.select(users.c.password_hash, users.c.password_enabled)
                .where(users.c.id == str(user_id))
            ).first()
        if row is None or not row[1]:
            return None
        return row[0] or None

    # ---- writes -----------------------------------------------------------------

    def create(self, *, email: str, name: str | None = "", role: str = "user",
               status: str = "active", password: str | None = None,
               password_hash: str | None = None, password_enabled: bool = True,
               source: str = "admin", id: str | None = None,  # noqa: A002
               now: float | None = None) -> dict:
        """Create an account. ``password`` is hashed here (Argon2id);
        ``password_hash`` stores an existing hash as is (migration). Raises
        AccountExists or InvalidAccount."""
        email = normalize_email(email)
        if not _EMAIL.match(email) or len(email) > 320:
            raise InvalidAccount("Enter a valid email address.")
        if role not in ROLES:
            raise InvalidAccount("The role must be admin or user.")
        if status not in STATUSES:
            raise InvalidAccount("The status must be active or pending.")
        if source not in SOURCES:
            raise InvalidAccount("Unknown account source.")
        user_id = str(id) if id else str(uuid.uuid4())
        if not _ID.match(user_id):
            raise InvalidAccount("Invalid account id.")
        name = (name or "").strip()
        if len(name) > 200:
            raise InvalidAccount("Use a name of at most 200 characters.")
        if password is not None and password_hash is not None:
            raise ValueError("pass a password or a hash, not both")
        stored = passwords.hash_password(password) if password is not None else password_hash
        if stored and not password_enabled:
            raise ValueError("an account without password sign-in has no password")
        ts = time.time() if now is None else now
        try:
            with self.engine.begin() as conn:
                conn.execute(users.insert().values(
                    id=user_id, email=email, name=name or None, role=role, status=status,
                    password_hash=stored or None, password_enabled=1 if password_enabled else 0,
                    source=source, created_at=ts, updated_at=ts, last_login_at=None,
                ))
        except IntegrityError as exc:
            raise AccountExists("An account with this email already exists.") from exc
        created = self.get(user_id)
        assert created is not None
        return created

    def _update(self, conn, user_id: str, **values) -> int:
        values["updated_at"] = time.time()
        return conn.execute(users.update().where(users.c.id == str(user_id)).values(**values)).rowcount

    def set_password(self, user_id: str, password: str | None = None, *,
                     password_hash: str | None = None,
                     except_token_hash: str | None = None) -> None:
        """Set (or, with neither argument, clear) the password. Ends every
        session except ``except_token_hash`` and cancels unused one-time links."""
        stored = passwords.hash_password(password) if password is not None else password_hash
        now = time.time()
        with self.engine.begin() as conn:
            done = self._update(conn, user_id, password_hash=stored or None,
                                **({"password_enabled": 1} if stored else {}))
            if not done:
                raise KeyError(user_id)
            self._revoke(conn, user_id, now, except_token_hash)
            conn.execute(links.delete().where(links.c.user_id == str(user_id),
                                              links.c.used_at.is_(None)))

    def rehash(self, user_id: str, new_hash: str, old_hash: str) -> float | None:
        """Replace a migrated or outdated hash after a successful sign-in,
        only if it is still the one that was verified. Sessions are kept.
        Returns the account's new ``updated_at``, or None when the hash had
        already changed (then the sign-in must not go ahead on the old one)."""
        now = time.time()
        with self.engine.begin() as conn:
            done = conn.execute(users.update().where(
                users.c.id == str(user_id), users.c.password_hash == old_hash,
            ).values(password_hash=new_hash, updated_at=now)).rowcount
        return now if done else None

    def set_name(self, user_id: str, name: str) -> dict:
        name = (name or "").strip()
        if not name or len(name) > 200:
            raise InvalidAccount("Enter a name of at most 200 characters.")
        with self.engine.begin() as conn:
            if not self._update(conn, user_id, name=name):
                raise KeyError(user_id)
        return self.get(user_id)  # type: ignore[return-value]

    def set_role(self, user_id: str, role: str) -> bool:
        """Returns whether it changed. A change ends the person's sessions."""
        if role not in ROLES:
            raise InvalidAccount("The role must be admin or user.")
        now = time.time()
        with self.engine.begin() as conn:
            row = conn.execute(sa.select(users.c.role).where(users.c.id == str(user_id))).first()
            if row is None:
                raise KeyError(user_id)
            if row[0] == role:
                return False
            self._update(conn, user_id, role=role)
            self._revoke(conn, user_id, now)
        return True

    def set_status(self, user_id: str, status: str) -> bool:
        """Returns whether it changed. Leaving ``active`` ends the sessions."""
        if status not in STATUSES:
            raise InvalidAccount("The status must be active or pending.")
        now = time.time()
        with self.engine.begin() as conn:
            row = conn.execute(sa.select(users.c.status).where(users.c.id == str(user_id))).first()
            if row is None:
                raise KeyError(user_id)
            if row[0] == status:
                return False
            self._update(conn, user_id, status=status)
            if status != "active":
                self._revoke(conn, user_id, now)
        return True

    def set_password_enabled(self, user_id: str, enabled: bool) -> None:
        """Turn password sign-in on or off. Off also clears the password."""
        with self.engine.begin() as conn:
            values = {"password_enabled": 1 if enabled else 0}
            if not enabled:
                values["password_hash"] = None
            if not self._update(conn, user_id, **values):
                raise KeyError(user_id)

    def touch_login(self, user_id: str, now: float | None = None) -> None:
        with self.engine.begin() as conn:
            conn.execute(users.update().where(users.c.id == str(user_id))
                         .values(last_login_at=time.time() if now is None else now))

    def delete(self, user_id: str) -> bool:
        """Delete the account with its sessions, identities and links."""
        user_id = str(user_id)
        with self.engine.begin() as conn:
            conn.execute(sessions.delete().where(sessions.c.user_id == user_id))
            conn.execute(identities.delete().where(identities.c.user_id == user_id))
            conn.execute(links.delete().where(links.c.user_id == user_id))
            return bool(conn.execute(users.delete().where(users.c.id == user_id)).rowcount)

    # ---- sessions (revocation lives here so every account change can use it) ----

    @staticmethod
    def _revoke(conn, user_id: str, now: float, except_token_hash: str | None = None) -> int:
        q = sessions.update().where(sessions.c.user_id == str(user_id),
                                    sessions.c.revoked_at.is_(None))
        if except_token_hash:
            q = q.where(sessions.c.token_hash != except_token_hash)
        return conn.execute(q.values(revoked_at=now)).rowcount

    def revoke_sessions(self, user_id: str, *, except_token_hash: str | None = None) -> int:
        with self.engine.begin() as conn:
            return self._revoke(conn, user_id, time.time(), except_token_hash)

    # ---- external sign-in identities --------------------------------------------

    def find_identity(self, issuer: str, subject: str) -> dict | None:
        with self.engine.connect() as conn:
            row = conn.execute(sa.select(identities).where(
                identities.c.issuer == issuer, identities.c.subject == subject)).first()
        return dict(row._mapping) if row else None

    def link_identity(self, *, provider: str, issuer: str, subject: str, user_id: str,
                      email: str | None) -> None:
        """Attach an external sign-in (issuer, subject) to an account. Raises
        AccountExists when that sign-in already belongs to someone."""
        try:
            with self.engine.begin() as conn:
                conn.execute(identities.insert().values(
                    provider=provider, issuer=issuer, subject=subject, user_id=str(user_id),
                    email=normalize_email(email) or None, created_at=time.time(),
                    last_login_at=None,
                ))
        except IntegrityError as exc:
            raise AccountExists("This sign-in is already linked to an account.") from exc

    def touch_identity(self, issuer: str, subject: str, email: str | None) -> None:
        with self.engine.begin() as conn:
            conn.execute(identities.update().where(
                identities.c.issuer == issuer, identities.c.subject == subject,
            ).values(last_login_at=time.time(), email=normalize_email(email) or None))

    def find_migrated_identity(self, provider: str, subject: str) -> dict | None:
        """An identity migrated from Open WebUI for this provider and subject,
        still under its placeholder issuer."""
        return self.find_identity(MIGRATED_ISSUER_PREFIX + provider, subject)

    def adopt_migrated_identity(self, provider: str, issuer: str, subject: str) -> dict | None:
        """Give a migrated identity its real issuer, now that the provider has
        signed this subject in. Idempotent and safe when two sign-ins race.
        Returns the identity under the real issuer, or None."""
        placeholder = MIGRATED_ISSUER_PREFIX + provider
        with self.engine.begin() as conn:
            row = conn.execute(sa.select(identities.c.user_id).where(
                identities.c.issuer == placeholder, identities.c.subject == subject,
            ).with_for_update()).first()
            if row is not None:
                taken = conn.execute(sa.select(identities.c.user_id).where(
                    identities.c.issuer == issuer, identities.c.subject == subject)).first()
                if taken is None:
                    conn.execute(identities.update().where(
                        identities.c.issuer == placeholder, identities.c.subject == subject,
                    ).values(issuer=issuer, provider=provider))
                else:
                    conn.execute(identities.delete().where(
                        identities.c.issuer == placeholder, identities.c.subject == subject))
        return self.find_identity(issuer, subject)

    def unlink_identity(self, issuer: str, subject: str) -> None:
        with self.engine.begin() as conn:
            conn.execute(identities.delete().where(
                identities.c.issuer == issuer, identities.c.subject == subject))

    def identities_for(self, user_id: str) -> list[dict]:
        with self.engine.connect() as conn:
            rows = conn.execute(sa.select(identities).where(
                identities.c.user_id == str(user_id))).fetchall()
        return [dict(r._mapping) for r in rows]


# ---- per-deployment access ------------------------------------------------------

_stores: dict[int, UserStore] = {}
_stores_lock = threading.Lock()


def store(hub_dir: Path) -> UserStore:
    """The account store for this deployment's operational database (cached)."""
    engine = engine_for(Path(hub_dir))
    key = id(engine)
    found = _stores.get(key)
    if found is None:
        with _stores_lock:
            found = _stores.get(key)
            if found is None:
                found = UserStore(engine)
                _stores[key] = found
    return found


def create(hub_dir: Path, **fields) -> dict:
    return store(hub_dir).create(**fields)


def get(hub_dir: Path, user_id: str | None) -> dict | None:
    return store(hub_dir).get(user_id)


def find_by_email(hub_dir: Path, email: str | None) -> dict | None:
    return store(hub_dir).find_by_email(email)


def list_users(hub_dir: Path) -> list[dict]:
    return store(hub_dir).list()


def set_password(hub_dir: Path, user_id: str, password: str | None = None, **kw) -> None:
    store(hub_dir).set_password(user_id, password, **kw)


def set_name(hub_dir: Path, user_id: str, name: str) -> dict:
    return store(hub_dir).set_name(user_id, name)


def set_role(hub_dir: Path, user_id: str, role: str) -> bool:
    return store(hub_dir).set_role(user_id, role)


def set_status(hub_dir: Path, user_id: str, status: str) -> bool:
    return store(hub_dir).set_status(user_id, status)


def delete(hub_dir: Path, user_id: str) -> bool:
    """Delete an account (``UserStore.delete``) and its personal connection
    tokens when the connections part is installed."""
    done = store(hub_dir).delete(user_id)
    if done:
        _drop_connector_tokens(Path(hub_dir), str(user_id))
    return done


def _drop_connector_tokens(hub_dir: Path, user_id: str) -> None:
    """``hubzoid.connectors.tokens.drop_user(hub_dir, user_id)``, when present.
    The account is already gone; a failure here is logged, never raised."""
    try:
        from ..connectors import tokens
    except ImportError as exc:
        if getattr(exc, "name", None) not in ("hubzoid.connectors.tokens", "hubzoid.connectors"):
            log.warning("auth: personal connection tokens of a deleted account were not removed "
                        "(the connections module did not load)")
        return
    drop = getattr(tokens, "drop_user", None)
    if not callable(drop):
        return
    try:
        drop(hub_dir, user_id)
    except Exception:  # noqa: BLE001
        log.warning("auth: personal connection tokens of a deleted account could not be removed",
                    exc_info=True)


def touch_login(hub_dir: Path, user_id: str) -> None:
    store(hub_dir).touch_login(user_id)


def admins(hub_dir: Path) -> list[dict]:
    """Active administrators who can sign in. With sign-in on, the local owner
    (which has no way to sign in) is not counted."""
    from .. import appmode

    return store(hub_dir).admins(exclude_local=appmode.auth_enabled(hub_dir))


def sync_identity(hub_dir: Path, user: Mapping) -> None:
    """Record the account on its access identity (``hz_identities``), the way
    Open WebUI accounts were recorded at sign-in: the People list shows it, and
    a pending account is unavailable until approved.

    An identity bound to a different account id is the email-reuse case: the
    access store removes that earlier account's grants and blocks the identity
    until an administrator reviews it (``upsert_identity``)."""
    from ..access import store_for

    store_for(Path(hub_dir)).upsert_identity(
        email=user["email"], owui_id=user["id"], display=user.get("name") or None,
        pending=user.get("status") == "pending",
    )


def on_sign_in(hub_dir: Path, user: Mapping) -> None:
    """What every verified sign-in records, as 1.0.x did at each verified Open
    WebUI session: the access identity (``sync_identity``), and for the
    deployment's configured owner, signing in as an administrator, the owner's
    grants on every hub, once (``provision_owner``). An arbitrary administrator
    never provisions itself."""
    from ..access.identity import normalize
    from ..access.session import configured_owner

    sync_identity(hub_dir, user)
    if user.get("role") == "admin" and normalize(user["email"]) == configured_owner(Path(hub_dir)):
        provision_owner(hub_dir, normalize(user["email"]))


def _identity_account_id(hub_dir: Path, email: str) -> str | None:
    """The account id recorded on the access identity for ``email``, if any."""
    from ..access import store_for

    return (store_for(Path(hub_dir)).identity(email) or {}).get("owui_id") or None


def _reusable_id(st: UserStore, hub_dir: Path, email: str) -> str | None:
    """The identity's recorded account id when no other account holds it."""
    recorded = _identity_account_id(hub_dir, email)
    if recorded and _ID.match(str(recorded)) and st.get(recorded) is None:
        return str(recorded)
    return None


def ensure_local_owner(hub_dir: Path) -> dict:
    """The local owner account (``admin@localhost``), created on first use.

    An existing account with that email (for example one migrated from Open
    WebUI) is the local owner, never a second one, so its id is stable across
    restarts. A new one takes the id already recorded for that email in
    ``hz_identities`` (an install that ran Open WebUI keeps its bindings), else
    a fresh one. In local mode it is always an active administrator."""
    from .. import appmode

    hub_dir = Path(hub_dir)
    st = store(hub_dir)
    user = st.find_by_email(LOCAL_OWNER_EMAIL)
    created = False
    if user is None:
        try:
            user = st.create(email=LOCAL_OWNER_EMAIL, name=LOCAL_OWNER_NAME, role="admin",
                             status="active", password_enabled=True, source="local",
                             id=_reusable_id(st, hub_dir, LOCAL_OWNER_EMAIL))
            created = True
        except AccountExists:
            user = st.find_by_email(LOCAL_OWNER_EMAIL)
            if user is None:  # the id was taken concurrently: take a fresh one
                user = st.create(email=LOCAL_OWNER_EMAIL, name=LOCAL_OWNER_NAME, role="admin",
                                 status="active", source="local")
                created = True
    if not appmode.auth_enabled(hub_dir) and (user["role"] != "admin" or user["status"] != "active"):
        st.set_role(user["id"], "admin")
        st.set_status(user["id"], "active")
        user = st.get(user["id"]) or user
    recorded = _identity_account_id(hub_dir, LOCAL_OWNER_EMAIL)
    if created or recorded != user["id"]:
        if recorded and recorded != user["id"]:
            # Binding would count as a replaced account and block the owner.
            log.warning("auth: the local owner's access identity is bound to another account id; "
                        "left as it is")
        else:
            sync_identity(hub_dir, user)
    return user


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def bootstrap_admin_from_env(hub_dir: Path, env: Mapping[str, str] | None = None) -> dict | None:
    """Create the first administrator from ``HUBZOID_ADMIN_EMAIL`` and
    ``HUBZOID_ADMIN_PASSWORD`` (or Open WebUI's ``WEBUI_ADMIN_EMAIL`` and
    ``WEBUI_ADMIN_PASSWORD``) when sign-in is on and no account exists yet
    (the local owner does not count). The new administrator gets the owner's
    grants on every hub of the deployment, once, like the configured owner at
    first sign-in. Returns the account, or None when nothing was created.

    Safe to call from every bridge at start: only one of them creates it."""
    from .. import appmode

    env = os.environ if env is None else env
    hub_dir = Path(hub_dir)
    email = normalize_email(env.get("HUBZOID_ADMIN_EMAIL") or env.get("WEBUI_ADMIN_EMAIL"))
    password = env.get("HUBZOID_ADMIN_PASSWORD") or env.get("WEBUI_ADMIN_PASSWORD") or ""
    if not email or not password or not appmode.auth_enabled(hub_dir, env):
        return None
    st = store(hub_dir)
    if st.count(exclude_local=True):
        return None
    try:
        passwords.check(password)
    except passwords.PasswordRejected:
        log.error("auth: the first administrator was not created: HUBZOID_ADMIN_PASSWORD must "
                  "be %d to %d characters", passwords.MIN_LENGTH, passwords.MAX_LENGTH)
        return None
    if not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", email):
        log.error("auth: the first administrator was not created: HUBZOID_ADMIN_EMAIL is not "
                  "an email address")
        return None
    name = (env.get("HUBZOID_ADMIN_NAME") or "").strip() or email.split("@", 1)[0]
    try:
        user = st.create(email=email, name=name, role="admin", status="active",
                         password=password, source="bootstrap",
                         id=_reusable_id(st, hub_dir, email))
    except AccountExists:
        return None  # another bridge created it first, or the email is taken
    sync_identity(hub_dir, user)
    provision_owner(hub_dir, email)
    log.info("auth: created the first administrator %s", email)
    return user


def provision_owner(hub_dir: Path, email: str) -> list[str]:
    """Give ``email`` the configured owner's grants on every hub of the
    deployment (once per hub; ``GrantStore.provision_owner``). Returns the hubs
    provisioned now."""
    from .. import deployment
    from ..access import store_for

    done: list[str] = []
    try:
        hubs = deployment.hubs(Path(hub_dir))
    except Exception:  # noqa: BLE001 — provisioning also runs at the owner's sign-in
        log.warning("auth: deployment hubs unreadable; owner grants not provisioned yet")
        return done
    for h in hubs:
        path = Path(h["path"])
        gs = store_for(path)
        # Cheap read first: provisioning takes the store's write lock, and it
        # runs at every verified request of the owner, as in 1.0.x.
        with gs.engine.connect() as conn:
            if gs._meta_get(conn, "initial_owner:" + str(h["key"]).strip().lower()):  # noqa: SLF001
                continue
        if gs.provision_owner(email, h["key"], fresh=(path / ".hubzoid" / "fresh-install").exists()):
            done.append(h["key"])
    return done


def mcp_account(hub_dir: Path, *, email: str | None = None,
                account_id: str | None = None) -> dict | None:
    """The account behind an MCP OAuth grant: ``{"account_id", "email"}`` for
    an existing, active, unblocked account whose access identity is bound to
    it, else None. Mirrors ``mcp_oauth.account`` for Open WebUI accounts."""
    from ..access import store_for

    try:
        st = store(hub_dir)
        user = st.get(account_id) if account_id else st.find_by_email(email)
        if user is None or user["status"] != "active":
            return None
        gs = store_for(Path(hub_dir))
        known = gs.identity(user["email"])
        if known and known.get("owui_id") and known["owui_id"] != user["id"]:
            return None
        if gs.is_suspended(user["email"]):
            return None
        return {"account_id": user["id"], "email": user["email"]}
    except Exception:  # noqa: BLE001 — fail closed
        log.warning("auth: MCP account lookup failed")
        return None
