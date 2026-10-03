"""One-time links to set or reset a password: ``/auth/set-password?token=``.

An administrator (Console "Add user" or "Reset password", or ``hubzoid admin``)
gets a link to share with the person. The token is 32 random bytes; only its
SHA-256 digest is stored (``hz_auth_links``). A link:

  * expires after 72 hours (``HUBZOID_LINK_HOURS``),
  * works once: using it marks it used in the same statement that checks it,
    so two tabs racing get one success,
  * is replaced by a newer link for the same person (issuing one cancels the
    person's unused links), and by any password change,
  * signs the person in when used, and ends their other sessions. The session
    starts only on the account exactly as the link left it: a reset, role
    change or block after the link was used means no session.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import sqlalchemy as sa

from . import sessions as sessionlib
from .schema import engine_for, links, sessions, users

PURPOSES = ("set_password", "reset_password")
PATH = "/auth/set-password"


class LinkInvalid(Exception):
    """The link is unknown, expired, used, or its account can't use it."""


def lifetime_seconds() -> int:
    try:
        hours = float(os.environ.get("HUBZOID_LINK_HOURS") or 72)
    except ValueError:
        hours = 72
    return int((hours if hours > 0 else 72) * 3600)


def url(token: str, base: str = "") -> str:
    """The link a person opens. ``base`` is the public URL, or '' for a
    same-origin path. Only its origin is used: the sign-in pages live at the
    site root, also when a gateway gives the hub a ``/b/<hub>`` public URL."""
    from urllib.parse import urlsplit

    parts = urlsplit(base.strip())
    origin = f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else base.rstrip("/")
    return f"{origin}{PATH}?token={token}"


def create(hub_dir: Path, user_id: str, *, purpose: str = "set_password",
           created_by: str | None = None) -> tuple[str, float]:
    """Issue a link for an account. Returns (token, expires_at). Earlier
    unused links for the same account stop working."""
    if purpose not in PURPOSES:
        raise ValueError("unknown link purpose")
    token = sessionlib.new_token()
    now = time.time()
    expires = now + lifetime_seconds()
    from .users import is_local_address

    with engine_for(Path(hub_dir)).begin() as conn:
        # The account row is locked (PostgreSQL; SQLite serializes writers), so
        # two links issued at once can't both survive.
        row = conn.execute(sa.select(users.c.id, users.c.email).where(users.c.id == str(user_id))
                           .with_for_update()).first()
        if row is None:
            raise KeyError(user_id)
        if is_local_address(row[1]):
            raise ValueError("the local owner has no password to set")
        conn.execute(links.delete().where(links.c.user_id == str(user_id),
                                          links.c.used_at.is_(None)))
        conn.execute(links.insert().values(
            token_hash=sessionlib.digest(token), user_id=str(user_id), purpose=purpose,
            created_by=(created_by or None) and str(created_by)[:320], created_at=now,
            expires_at=expires, used_at=None,
        ))
    return token, expires


def _row(conn, token: str):
    return conn.execute(
        sa.select(links.c.user_id, links.c.purpose, links.c.expires_at, links.c.used_at,
                  users.c.email, users.c.status, users.c.password_enabled)
        .select_from(links.join(users, users.c.id == links.c.user_id))
        .where(links.c.token_hash == sessionlib.digest(token))
    ).first()


def _usable(hub_dir: Path, m, now: float) -> bool:
    from .users import is_local_address

    if m["used_at"] is not None or m["expires_at"] <= now or is_local_address(m["email"]):
        return False
    if m["status"] != "active" or not m["password_enabled"]:
        return False
    from ..access import store_for

    return not store_for(Path(hub_dir)).is_suspended(m["email"])


def inspect(hub_dir: Path, token: str) -> dict:
    """``{"valid", "purpose", "email"}`` for the set-password page. An invalid
    link reveals nothing about any account."""
    if not token or len(token) > 256:
        return {"valid": False, "purpose": None, "email": None}
    with engine_for(Path(hub_dir)).connect() as conn:
        row = _row(conn, token)
    if row is None or not _usable(hub_dir, row._mapping, time.time()):
        return {"valid": False, "purpose": None, "email": None}
    m = row._mapping
    return {"valid": True, "purpose": m["purpose"], "email": m["email"]}


def consume(hub_dir: Path, token: str, *, password: str) -> dict:
    """Use the link to set ``password``: returns ``{"user_id", "email",
    "purpose", "updated_at"}``. Raises PasswordRejected (the link stays
    usable) or LinkInvalid.

    In one transaction: the link is claimed, the password is stored, every
    session of the account ends and its other unused links are cancelled. The
    caller then starts the new session, expecting exactly ``updated_at``, the
    account version this transaction wrote (``sessions.create_session``), so a
    reset or block committed in between leaves no session."""
    from . import passwords

    passwords.check(password)
    if not token or len(token) > 256:
        raise LinkInvalid()
    with engine_for(Path(hub_dir)).connect() as conn:  # cheap refusal before hashing
        row = _row(conn, token)
    if row is None or not _usable(hub_dir, row._mapping, time.time()):
        raise LinkInvalid()
    stored = passwords.hash_password(password)
    now = time.time()
    digest = sessionlib.digest(token)
    with engine_for(Path(hub_dir)).begin() as conn:
        row = _row(conn, token)
        if row is None or not _usable(hub_dir, row._mapping, now):
            raise LinkInvalid()
        claimed = conn.execute(links.update().where(
            links.c.token_hash == digest, links.c.used_at.is_(None), links.c.expires_at > now,
        ).values(used_at=now)).rowcount
        if claimed != 1:
            raise LinkInvalid()
        m = row._mapping
        conn.execute(users.update().where(users.c.id == m["user_id"]).values(
            password_hash=stored, password_enabled=1, updated_at=now))
        conn.execute(sessions.update().where(
            sessions.c.user_id == m["user_id"], sessions.c.revoked_at.is_(None),
        ).values(revoked_at=now))
        conn.execute(links.delete().where(
            links.c.user_id == m["user_id"], links.c.used_at.is_(None),
            links.c.token_hash != digest))
        return {"user_id": m["user_id"], "email": m["email"], "purpose": m["purpose"],
                "updated_at": now}
