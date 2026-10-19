"""Directory providers, provisioned accounts, SCIM tokens and SAML replay.

Tables from migration ``op_0033``, in the shared operational store.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
import uuid
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from ..schema import engine_for, metadata, users

providers = sa.Table(
    "hz_directory_providers", metadata,
    sa.Column("id", sa.String(32), primary_key=True),
    sa.Column("kind", sa.String(16), nullable=False),
    sa.Column("name", sa.String(120), nullable=False),
    sa.Column("enabled", sa.Integer, nullable=False),
    sa.Column("config", sa.Text, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("updated_at", sa.Float, nullable=False),
    sa.Column("updated_by", sa.String(320)),
)
accounts = sa.Table(
    "hz_directory_accounts", metadata,
    sa.Column("id", sa.String(40), primary_key=True),
    sa.Column("provider_id", sa.String(32), nullable=False),
    sa.Column("user_id", sa.String(64), nullable=False),
    sa.Column("user_name", sa.String(320), nullable=False),
    sa.Column("external_id", sa.String(255)),
    sa.Column("active", sa.Integer, nullable=False),
    sa.Column("resource", sa.Text, nullable=False),
    sa.Column("version", sa.Integer, nullable=False),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("updated_at", sa.Float, nullable=False),
    sa.Column("deleted_at", sa.Float),
)
scim_tokens = sa.Table(
    "hz_scim_tokens", metadata,
    sa.Column("id", sa.String(32), primary_key=True),
    sa.Column("provider_id", sa.String(32), nullable=False),
    sa.Column("name", sa.String(120), nullable=False),
    sa.Column("prefix", sa.String(24), nullable=False),
    sa.Column("token_hash", sa.String(64), nullable=False),
    sa.Column("created_by", sa.String(320)),
    sa.Column("created_at", sa.Float, nullable=False),
    sa.Column("last_used_at", sa.Float),
    sa.Column("revoked_at", sa.Float),
)
replay = sa.Table(
    "hz_saml_replay", metadata,
    sa.Column("provider_id", sa.String(32), primary_key=True),
    sa.Column("assertion_hash", sa.String(64), primary_key=True),
    sa.Column("expires_at", sa.Float, nullable=False),
)

PROVIDER_ID = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
DISABLED = "disabled"


class DirectoryError(ValueError):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _engine(hub_dir: Path):
    return engine_for(Path(hub_dir))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


# ---- providers ------------------------------------------------------------------

def _provider(row) -> dict:
    m = dict(row._mapping)
    m["config"] = json.loads(m["config"] or "{}")
    m["enabled"] = bool(m["enabled"])
    return m


def list_providers(hub_dir: Path, *, enabled_only: bool = False) -> list[dict]:
    q = sa.select(providers).order_by(providers.c.name)
    if enabled_only:
        q = q.where(providers.c.enabled == 1)
    with _engine(hub_dir).connect() as conn:
        return [_provider(r) for r in conn.execute(q)]


def get_provider(hub_dir: Path, provider_id: str) -> dict | None:
    with _engine(hub_dir).connect() as conn:
        row = conn.execute(sa.select(providers).where(providers.c.id == str(provider_id))).first()
    return _provider(row) if row else None


def save_provider(hub_dir: Path, *, provider_id: str, name: str, config: dict,
                  actor: str, enabled: bool | None = None) -> dict:
    if not PROVIDER_ID.match(provider_id or ""):
        raise DirectoryError(422, "invalid_provider",
                             "Use a short id of lowercase letters, digits and dashes, like okta.")
    name = (name or "").strip()
    if not name or len(name) > 120:
        raise DirectoryError(422, "invalid_name", "Give the provider a name people will recognise.")
    now = time.time()
    with _engine(hub_dir).begin() as conn:
        found = conn.execute(sa.select(providers.c.enabled).where(
            providers.c.id == provider_id)).first()
        values = {"name": name, "config": json.dumps(config), "updated_at": now, "updated_by": actor}
        if enabled is not None:
            values["enabled"] = 1 if enabled else 0
        if found:
            conn.execute(providers.update().where(providers.c.id == provider_id).values(**values))
        else:
            conn.execute(providers.insert().values(id=provider_id, kind="saml", created_at=now,
                                                   **{"enabled": 0, **values}))
    return get_provider(hub_dir, provider_id)  # type: ignore[return-value]


def delete_provider(hub_dir: Path, provider_id: str) -> bool:
    with _engine(hub_dir).begin() as conn:
        live = conn.execute(sa.select(sa.func.count()).select_from(accounts).where(
            accounts.c.provider_id == provider_id, accounts.c.deleted_at.is_(None))).scalar()
        if live:
            raise DirectoryError(409, "provider_in_use",
                                 "People are still provisioned by this provider. Turn it off instead.")
        conn.execute(scim_tokens.delete().where(scim_tokens.c.provider_id == provider_id))
        return bool(conn.execute(providers.delete().where(providers.c.id == provider_id)).rowcount)


# ---- SCIM tokens ------------------------------------------------------------------

def create_token(hub_dir: Path, provider_id: str, *, name: str, actor: str) -> tuple[dict, str]:
    token = f"hzs_{secrets.token_urlsafe(36)}"
    row = {"id": secrets.token_hex(8), "provider_id": provider_id, "name": (name or "SCIM")[:120],
           "prefix": token[:12], "token_hash": _digest(token), "created_by": actor,
           "created_at": time.time(), "last_used_at": None, "revoked_at": None}
    with _engine(hub_dir).begin() as conn:
        conn.execute(scim_tokens.insert().values(**row))
    return {k: row[k] for k in ("id", "name", "prefix", "created_at")}, token


def list_tokens(hub_dir: Path, provider_id: str) -> list[dict]:
    with _engine(hub_dir).connect() as conn:
        rows = conn.execute(sa.select(scim_tokens).where(scim_tokens.c.provider_id == provider_id)
                            .order_by(scim_tokens.c.created_at.desc())).fetchall()
    return [{k: r._mapping[k] for k in ("id", "name", "prefix", "created_at", "last_used_at",
                                        "revoked_at")} for r in rows]


def revoke_token(hub_dir: Path, provider_id: str, token_id: str) -> bool:
    with _engine(hub_dir).begin() as conn:
        return bool(conn.execute(scim_tokens.update().where(
            scim_tokens.c.id == token_id, scim_tokens.c.provider_id == provider_id,
            scim_tokens.c.revoked_at.is_(None)).values(revoked_at=time.time())).rowcount)


def token_provider(hub_dir: Path, token: str) -> str | None:
    """The provider a live SCIM token belongs to."""
    if not token or not token.startswith("hzs_") or len(token) > 200:
        return None
    with _engine(hub_dir).connect() as conn:
        row = conn.execute(sa.select(scim_tokens.c.id, scim_tokens.c.provider_id,
                                     scim_tokens.c.last_used_at).where(
            scim_tokens.c.token_hash == _digest(token), scim_tokens.c.revoked_at.is_(None))).first()
    if row is None:
        return None
    if not row[2] or time.time() - row[2] > 60:
        with _engine(hub_dir).begin() as conn:
            conn.execute(scim_tokens.update().where(scim_tokens.c.id == row[0])
                         .values(last_used_at=time.time()))
    return row[1]


# ---- provisioned accounts ----------------------------------------------------------

def _account(row) -> dict:
    m = dict(row._mapping)
    m["resource"] = json.loads(m["resource"] or "{}")
    m["active"] = bool(m["active"])
    return m


def accounts_for(hub_dir: Path, provider_id: str) -> list[dict]:
    with _engine(hub_dir).connect() as conn:
        rows = conn.execute(sa.select(accounts).where(
            accounts.c.provider_id == provider_id, accounts.c.deleted_at.is_(None))
            .order_by(accounts.c.user_name)).fetchall()
    return [_account(r) for r in rows]


def get_account(hub_dir: Path, provider_id: str, scim_id: str) -> dict | None:
    with _engine(hub_dir).connect() as conn:
        row = conn.execute(sa.select(accounts).where(
            accounts.c.provider_id == provider_id, accounts.c.id == str(scim_id),
            accounts.c.deleted_at.is_(None))).first()
    return _account(row) if row else None


def by_user_name(hub_dir: Path, provider_id: str, user_name: str) -> list[dict]:
    with _engine(hub_dir).connect() as conn:
        rows = conn.execute(sa.select(accounts).where(
            accounts.c.provider_id == provider_id, accounts.c.deleted_at.is_(None),
            sa.func.lower(accounts.c.user_name) == (user_name or "").strip().lower())).fetchall()
    return [_account(r) for r in rows]


def by_external_id(hub_dir: Path, provider_id: str, external_id: str) -> list[dict]:
    if not external_id:
        return []
    with _engine(hub_dir).connect() as conn:
        rows = conn.execute(sa.select(accounts).where(
            accounts.c.provider_id == provider_id, accounts.c.deleted_at.is_(None),
            accounts.c.external_id == str(external_id))).fetchall()
    return [_account(r) for r in rows]


def for_name_id(hub_dir: Path, provider_id: str, name_id: str) -> list[dict]:
    """The directory person a SAML NameID names: by the directory's own
    immutable id (``externalId``, for a persistent NameID) first, else by
    ``userName`` (an email NameID). Unique among live records either way."""
    return by_external_id(hub_dir, provider_id, name_id) or by_user_name(hub_dir, provider_id, name_id)


def _set_user_state(conn, user_id: str, active: bool, now: float) -> None:
    """The account follows the directory: off ends every credential at once."""
    from ..users import UserStore

    row = conn.execute(sa.select(users.c.status).where(users.c.id == user_id)).first()
    if row is None:
        return
    if active and row[0] == DISABLED:
        conn.execute(users.update().where(users.c.id == user_id)
                     .values(status="active", updated_at=now))
    elif not active and row[0] != DISABLED:
        conn.execute(users.update().where(users.c.id == user_id)
                     .values(status=DISABLED, updated_at=now))
        UserStore._revoke(conn, user_id, now, credentials=True)  # noqa: SLF001


def provision(hub_dir: Path, provider_id: str, *, user_name: str, email: str, name: str,
              external_id: str | None, active: bool, resource: dict) -> dict:
    """Create a managed account for a new directory person. Refuses an email
    or userName already in use (no account is taken over by email)."""
    from .. import users as userlib

    if by_user_name(hub_dir, provider_id, user_name):
        raise DirectoryError(409, "uniqueness", "A user with this userName already exists.")
    st = userlib.store(Path(hub_dir))
    if st.find_by_email(email) is not None:
        raise DirectoryError(409, "uniqueness", "An account with this email already exists in "
                                                 "Hubzoid. It isn't joined to the directory automatically.")
    created = st.create(email=email, name=name or email.split("@", 1)[0], role="user",
                        status="active", password_enabled=False, source="admin")
    scim_id = str(uuid.uuid4())
    now = time.time()
    resource = {**resource, "id": scim_id}
    with _engine(hub_dir).begin() as conn:
        conn.execute(users.update().where(users.c.id == created["id"]).values(managed_by=provider_id))
        conn.execute(accounts.insert().values(
            id=scim_id, provider_id=provider_id, user_id=created["id"], user_name=user_name,
            external_id=external_id, active=1 if active else 0, resource=json.dumps(resource),
            version=1, created_at=now, updated_at=now, deleted_at=None))
        if not active:
            _set_user_state(conn, created["id"], False, now)
    userlib.sync_identity(Path(hub_dir), st.get(created["id"]) or created)
    return get_account(hub_dir, provider_id, scim_id)  # type: ignore[return-value]


def update(hub_dir: Path, provider_id: str, scim_id: str, *, user_name: str, name: str | None,
           external_id: str | None, active: bool, resource: dict,
           expected_version: int | None = None) -> dict:
    """Replace a directory person. ``expected_version``: the version the
    change was made from; a write over a newer version (a concurrent
    deactivation) is refused with 412, never applied over it."""
    current = get_account(hub_dir, provider_id, scim_id)
    if current is None:
        raise DirectoryError(404, "not_found", "No such user.")
    clash = [a for a in by_user_name(hub_dir, provider_id, user_name) if a["id"] != scim_id]
    if clash:
        raise DirectoryError(409, "uniqueness", "Another user has this userName.")
    now = time.time()
    resource = {**resource, "id": scim_id}
    version = current["version"] if expected_version is None else int(expected_version)
    with _engine(hub_dir).begin() as conn:
        changed = conn.execute(accounts.update().where(
            accounts.c.id == scim_id, accounts.c.version == version,
            accounts.c.deleted_at.is_(None)).values(
            user_name=user_name, external_id=external_id, active=1 if active else 0,
            resource=json.dumps(resource), version=accounts.c.version + 1, updated_at=now)).rowcount
        if not changed:
            raise DirectoryError(412, "precondition", "The user changed since you read it.")
        if name:
            conn.execute(users.update().where(users.c.id == current["user_id"])
                         .values(name=name[:200], updated_at=now))
        _set_user_state(conn, current["user_id"], active, now)
    return get_account(hub_dir, provider_id, scim_id)  # type: ignore[return-value]


def deprovision(hub_dir: Path, provider_id: str, scim_id: str) -> bool:
    """DELETE from the directory: the account is turned off for good (its
    credentials end now), and kept with its conversations."""
    current = get_account(hub_dir, provider_id, scim_id)
    if current is None:
        return False
    now = time.time()
    with _engine(hub_dir).begin() as conn:
        conn.execute(accounts.update().where(accounts.c.id == scim_id)
                     .values(deleted_at=now, active=0, updated_at=now,
                             version=accounts.c.version + 1))
        _set_user_state(conn, current["user_id"], False, now)
    return True


# ---- SAML replay -------------------------------------------------------------------

def first_use(hub_dir: Path, provider_id: str, assertion_id: str, expires_at: float) -> bool:
    """True the first time an assertion id is seen; False on a replay."""
    now = time.time()
    with _engine(hub_dir).begin() as conn:
        conn.execute(replay.delete().where(replay.c.expires_at < now))
    try:
        with _engine(hub_dir).begin() as conn:
            conn.execute(replay.insert().values(provider_id=provider_id,
                                                assertion_hash=_digest(assertion_id),
                                                expires_at=max(expires_at, now + 300)))
    except IntegrityError:
        return False
    return True
