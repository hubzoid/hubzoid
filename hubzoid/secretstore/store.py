"""The secret store: write-only values in ``hz_secrets``, encrypted with the
deployment key.

Values go in through ``put`` and come out only through ``values`` and
``value``, for the bridge's start-up layer and ``secret:`` references. A removed
secret keeps its row with no value, so a revision never repeats: a stale
editor or a running bridge cannot mistake a re-created secret for the old one. No
function here returns a value with its metadata, and nothing logs one. Every
change writes one row to the access audit log in the same transaction, with
the name, scope and actor, never the value.

The routes and the CLI decide who may call these. This module checks only the
shape of what it is given and the expected revision.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from .. import secretbox
from ..access.identity import normalize
from . import names
from .refs import SCOPES

log = logging.getLogger("hubzoid.secretstore")

MAX_VALUE_BYTES = 32 * 1024
ORG = "*"  # the access audit's organization-wide scope (access.store.ORG)

_COLUMNS = ("scope", "target", "name", "restricted", "destination", "revision",
            "created_by", "created_at", "updated_by", "updated_at")
_LIVE = " AND value_enc IS NOT NULL"


class SecretError(Exception):
    """A refused store operation. ``code`` is stable snake_case, ``message`` is
    safe to show (never a value), ``status`` is the HTTP status for it."""

    def __init__(self, code: str, message: str, status: int = 400, **extra):
        super().__init__(message)
        self.code, self.message, self.status, self.extra = code, message, status, extra


@dataclass(frozen=True)
class Meta:
    scope: str
    target: str
    name: str
    restricted: bool
    destination: str | None
    revision: int
    created_by: str
    created_at: float
    updated_by: str
    updated_at: float

    def public(self) -> dict:
        """What any authorized listing may show: never the value."""
        return {"name": self.name, "scope": self.scope, "target": self.target,
                "restricted": self.restricted, "revision": self.revision, "set": True,
                "updated_by": self.updated_by, "updated_at": _iso(self.updated_at),
                "bound": self.destination is not None}


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def _meta(row) -> Meta:
    r = dict(zip(_COLUMNS, row))
    return Meta(scope=r["scope"], target=r["target"], name=r["name"], restricted=bool(r["restricted"]),
                destination=r["destination"], revision=int(r["revision"]), created_by=r["created_by"],
                created_at=float(r["created_at"]), updated_by=r["updated_by"],
                updated_at=float(r["updated_at"]))


def engine(hub_dir):
    """The shared operational store, migrated, as the connectors use it."""
    from .. import db, migrations

    eng = db.operational_engine(Path(hub_dir))
    migrations.upgrade(eng, "operational")
    return eng


def existing_engine(hub_dir):
    """The operational store when it already has ``hz_secrets``, else None.
    Never creates a database or runs migrations (doctor, read-only callers)."""
    from sqlalchemy import inspect

    from .. import db

    try:
        url = db.operational_url(Path(hub_dir))
        if url.startswith("sqlite:///") and not Path(url[len("sqlite:///"):]).exists():
            return None
        eng = db.operational_engine(Path(hub_dir))
        return eng if inspect(eng).has_table("hz_secrets") else None
    except Exception:  # noqa: BLE001 - an unreachable store reads as empty here
        log.debug("operational store not readable for secrets", exc_info=True)
        return None


def _check_scope(scope: str, target: str) -> None:
    if scope not in SCOPES:
        raise SecretError("invalid_request", "The scope is deployment, hub or channel.", 422)
    if scope == "deployment" and target:
        raise SecretError("invalid_request", "A deployment secret has no target.", 422)
    if scope == "hub" and (not target or "/" in target):
        raise SecretError("invalid_request", "A hub secret names its hub.", 422)
    if scope == "channel" and target.count("/") != 1:
        raise SecretError("invalid_request", "A channel secret names its hub and channel.", 422)


def _check_value(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise SecretError("validation_failed", "Enter a value. To remove a secret, delete it.", 422,
                          fields={"value": "required"})
    if "\x00" in value:
        raise SecretError("validation_failed", "The value contains a NUL character.", 422,
                          fields={"value": "invalid"})
    if len(value.encode()) > MAX_VALUE_BYTES:
        raise SecretError("validation_failed", f"The value is longer than {MAX_VALUE_BYTES // 1024} KiB.",
                          413, fields={"value": "too_long"})


def _audit(conn, hub_dir, actor: str, action: str, meta_scope: str, target: str, name: str,
           surface: str, request_id: str | None) -> None:
    from ..access import store_for

    hub = ORG if meta_scope == "deployment" else target.split("/", 1)[0]
    store_for(Path(hub_dir)).write_audit(conn, normalize(actor) or "unknown", action,
                                         subject=f"{meta_scope}:{name}", hub=hub,
                                         permission="secrets.manage", surface=surface,
                                         request_id=request_id)


def _select(conn, scope: str, target: str, name: str, *, live: bool = True):
    """The row with its ``value_enc`` last (null for a removed secret). With
    ``live`` a removed secret reads as absent."""
    return conn.execute(text("SELECT " + ", ".join(_COLUMNS) + ", value_enc FROM hz_secrets "
                             "WHERE scope = :s AND target = :t AND name = :n" + (_LIVE if live else "")),
                        {"s": scope, "t": target, "n": name}).first()


def _conflict(current: int | None) -> SecretError:
    if current is None:
        return SecretError("revision_conflict", "This secret was removed since you loaded it. Reload and "
                           "set it again.", 409, current_revision=None)
    return SecretError("revision_conflict", "Someone changed this secret since you loaded it. Reload and "
                       "set it again.", 409, current_revision=current)


def put(hub_dir, *, scope: str, target: str = "", name: str, value: str, actor: str,
        restricted: bool | None = None, destination: str | None = None,
        expected_revision: int | str | None = None, surface: str = "console",
        request_id: str | None = None) -> Meta:
    """Set or replace one secret. ``expected_revision`` ``"absent"`` means
    create only, an integer means replace only that revision, None means
    either. ``restricted`` None keeps a stored secret's restriction (a new one
    is not restricted), so rotating a value never declassifies it. Returns the
    new metadata."""
    _check_scope(scope, target)
    why = names.problem(name)
    if why:
        raise SecretError("validation_failed", why, 422, fields={"name": "invalid"})
    _check_value(value)
    if restricted and scope != "hub":
        raise SecretError("invalid_request", "Only a hub secret can be restricted.", 422)
    token = secretbox.encrypt(Path(hub_dir), value)
    now = time.time()
    who = normalize(actor) or "unknown"
    eng = engine(hub_dir)
    try:
        with eng.begin() as conn:
            row = _select(conn, scope, target, name, live=False)
            live = row is not None and row[-1] is not None
            current = int(row[5]) if live else None
            if expected_revision == "absent" and live:
                raise _conflict(current)
            if isinstance(expected_revision, int) and current != expected_revision:
                raise _conflict(current)
            keep = bool(row[3]) if live else False
            restricted = keep if restricted is None else bool(restricted)
            if row is None:
                conn.execute(text(
                    "INSERT INTO hz_secrets (scope, target, name, restricted, value_enc, destination, revision, "
                    "created_by, created_at, updated_by, updated_at) "
                    "VALUES (:s, :t, :n, :r, :v, :d, 1, :w, :at, :w, :at)"),
                    {"s": scope, "t": target, "n": name, "r": int(restricted), "v": token,
                     "d": destination, "w": who, "at": now})
            else:
                done = conn.execute(text(
                    "UPDATE hz_secrets SET restricted = :r, value_enc = :v, destination = :d, "
                    "revision = revision + 1, updated_by = :w, updated_at = :at "
                    "WHERE scope = :s AND target = :t AND name = :n AND revision = :rev"),
                    {"s": scope, "t": target, "n": name, "r": int(restricted), "v": token,
                     "d": destination, "w": who, "at": now, "rev": int(row[5])})
                if done.rowcount != 1:
                    raise _conflict(None)
            _audit(conn, hub_dir, who, "secret_set", scope, target, name, surface, request_id)
            return _meta(_select(conn, scope, target, name)[:-1])
    except IntegrityError:
        # Two writers created the same secret at once: this one lost.
        raise _conflict(None) from None


def delete(hub_dir, *, scope: str, target: str = "", name: str, actor: str,
           expected_revision: int | None = None, surface: str = "console",
           request_id: str | None = None) -> Meta:
    """Remove one secret. Returns the metadata it had."""
    _check_scope(scope, target)
    who = normalize(actor) or "unknown"
    with engine(hub_dir).begin() as conn:
        row = _select(conn, scope, target, name)
        if row is None:
            raise SecretError("not_found", f"No stored secret named {name}.", 404)
        meta = _meta(row[:-1])
        if expected_revision is not None and meta.revision != expected_revision:
            raise _conflict(meta.revision)
        # Keep the row without its value, so the next revision is never reused.
        done = conn.execute(text(
            "UPDATE hz_secrets SET value_enc = NULL, destination = NULL, revision = revision + 1, "
            "updated_by = :w, updated_at = :at "
            "WHERE scope = :s AND target = :t AND name = :n AND revision = :rev AND value_enc IS NOT NULL"),
            {"s": scope, "t": target, "n": name, "w": who, "at": time.time(), "rev": meta.revision})
        if done.rowcount != 1:
            raise _conflict(None)
        _audit(conn, hub_dir, who, "secret_delete", scope, target, name, surface, request_id)
        return meta


def list_meta(hub_dir, *, scope: str, target: str = "", eng=None) -> list[Meta]:
    """Metadata of every secret in one scope and target, by name."""
    eng = eng if eng is not None else existing_engine(hub_dir)
    if eng is None:
        return []
    with eng.connect() as conn:
        rows = conn.execute(text("SELECT " + ", ".join(_COLUMNS) + " FROM hz_secrets "
                                 "WHERE scope = :s AND target = :t" + _LIVE + " ORDER BY name"),
                            {"s": scope, "t": target}).all()
    return [_meta(r) for r in rows]


def get_meta(hub_dir, *, scope: str, target: str = "", name: str) -> Meta | None:
    eng = existing_engine(hub_dir)
    if eng is None:
        return None
    with eng.connect() as conn:
        row = _select(conn, scope, target, name)
    return _meta(row[:-1]) if row else None


@dataclass(frozen=True)
class Stored:
    """One decrypted value, for the start-up layer only. ``value`` is None when
    the deployment key cannot decrypt it (``unreadable``)."""
    meta: Meta
    value: str | None

    def __repr__(self) -> str:  # never print a value by accident
        return f"Stored({self.meta.scope}:{self.meta.target}:{self.meta.name}, readable={self.value is not None})"


def values(hub_dir, *, scope: str, target: str = "", eng=None) -> list[Stored]:
    """Every secret in one scope and target, decrypted. A value the deployment
    key cannot read comes back as None, so the caller can report it."""
    eng = eng if eng is not None else existing_engine(hub_dir)
    if eng is None:
        return []
    with eng.connect() as conn:
        rows = conn.execute(text("SELECT " + ", ".join(_COLUMNS) + ", value_enc FROM hz_secrets "
                                 "WHERE scope = :s AND target = :t" + _LIVE + " ORDER BY name"),
                            {"s": scope, "t": target}).all()
    out: list[Stored] = []
    for row in rows:
        meta = _meta(row[:-1])
        try:
            value = secretbox.decrypt_text(Path(hub_dir), row[-1])
        except secretbox.SecretKeyError:
            value = None
        out.append(Stored(meta, value))
    return out


def value(hub_dir, *, scope: str, target: str = "", name: str,
          env: Mapping[str, str] | None = None) -> str | None:
    """One decrypted value, for ``secret:`` references. None when absent.
    Raises SecretError ``secret_rebind`` when the value is bound to a
    destination that ``env`` (default: the process environment) no longer
    names, as the start-up layer does, and ``secret_unreadable`` when the key
    cannot decrypt it."""
    eng = existing_engine(hub_dir)
    if eng is None:
        return None
    with eng.connect() as conn:
        row = conn.execute(text("SELECT value_enc, destination FROM hz_secrets WHERE scope = :s "
                                "AND target = :t AND name = :n" + _LIVE),
                           {"s": scope, "t": target, "n": name}).first()
    if row is None:
        return None
    if row[1] is not None and names.destination(name, os.environ if env is None else env) != row[1]:
        keys = names.BINDINGS.get(name, ())
        changed = f" ({' or '.join(keys)} changed)" if keys else ""
        raise SecretError("secret_rebind", f"The stored secret {name} was set for a different "
                          f"destination{changed}. Set it again for this one with hubzoid secrets set.", 503)
    try:
        return secretbox.decrypt_text(Path(hub_dir), row[0])
    except secretbox.SecretKeyError:
        raise SecretError("secret_unreadable", f"The stored secret {name} cannot be read with this "
                          "deployment key. Set it again.", 503) from None
