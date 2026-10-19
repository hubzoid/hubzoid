"""Personal API keys: a script, CI job or the remote CLI acting as one person.

A key is ``hzp_<id>_<secret>``. Only its SHA-256 digest and a display prefix
are stored (``hz_api_keys``), so the full key is shown once, when it is made.
It is sent as ``Authorization: Bearer hzp_...``.

What a key can do is the smallest of:

  * its owner's live access (an account that is blocked, deleted or whose
    access was removed takes its keys with it),
  * the credential counter it was made under (``auth_epoch``: "revoke all
    credentials", a password reset or a block ends it),
  * its expiry,
  * its operations (stable ids, fixed when the key is made):
      - ``chat``: talk to agents: ``/api/chat``, ``/api/conversations``,
        ``/v1/chat/completions``, ``/v1/models``, the agent list and ``/api/me``;
      - ``read``: any read (GET) the owner can make;
      - ``manage``: any other change the owner can make,
  * and, when chosen, a list of agents (hubs).

Some things a key can never do, whatever its operations: make or revoke keys,
change two-step sign-in or the password, prove a recent sign-in, approve
requests (``/change-requests/.../confirm``) or sign out other devices.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from pathlib import Path

import sqlalchemy as sa

from .schema import api_keys, engine_for, users

PREFIX = "hzp_"
OPERATIONS = ("chat", "read", "manage")
PRESETS = {
    "chat": ("chat",),
    "read": ("read",),
    "all": ("chat", "read", "manage"),
}
MAX_KEYS = 20
MAX_LIFETIME_DAYS = 366
TOUCH_SECONDS = 60
_ID = re.compile(r"^[A-Za-z0-9]{8,32}$")

# Never with a key, whatever its operations (method, path prefix).
_NEVER = (
    ("POST", "/api/me/api-keys"), ("DELETE", "/api/me/api-keys"),
    ("", "/api/auth/mfa"), ("", "/api/me/mfa"), ("", "/api/auth/reauth"),
    ("", "/api/auth/password"), ("", "/api/auth/login"), ("", "/api/auth/logout"),
    ("POST", "/api/me/sessions"), ("DELETE", "/api/me/sessions"),
    ("", "/api/me/identity-links"), ("", "/oauth/"),
)
# Paths that run in one agent: a key limited to some agents reaches only theirs.
HUB_PATHS = ("/api/chat", "/api/conversations", "/v1/", "/api/shares", "/api/models")
# A key limited to some agents uses the OpenAI-compatible API, which runs in
# exactly one agent, and reads who it is. Conversations and files span agents.
LIMITED_PATHS = ("/v1/chat/completions", "/v1/models", "/api/auth/session", "/api/me/access")
# ``chat`` reads the chat app and writes only the person's own chats, files
# and organizing: never the workspace's chat settings or imports.
_CHAT = ("/api/chat", "/api/conversations", "/v1/chat/completions", "/v1/models",
         "/api/me", "/api/agents", "/api/models", "/api/shares", "/api/auth/session")
_CHAT_WRITES = ("/v1/chat/completions", "/api/conversations", "/api/shares", "/api/chat/files",
                "/api/chat/bookmarks", "/api/chat/folders", "/api/chat/tags",
                "/api/chat/preferences", "/api/chat/notifications",
                "/api/chat/notification-presence", "/api/chat/prompts", "/api/chat/presets")


class KeyRefused(ValueError):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def looks_like_key(token: str | None) -> bool:
    return bool(token) and token.startswith(PREFIX) and len(token) < 200


def _public(row) -> dict:
    m = row._mapping
    return {"id": m["id"], "name": m["name"], "prefix": m["prefix"],
            "operations": json.loads(m["scopes"] or "[]"),
            "hubs": json.loads(m["hubs"]) if m["hubs"] else None,
            "created_at": m["created_at"], "expires_at": m["expires_at"],
            "last_used_at": m["last_used_at"], "revoked_at": m["revoked_at"]}


def create(hub_dir: Path, user, *, name: str, operations, hubs=None,
           expires_in_days: int | None = 90) -> tuple[dict, str]:
    """Make a key for ``user`` (an AuthUser). Returns (its details, the key,
    shown this once)."""
    name = (name or "").strip()
    if not name or len(name) > 120:
        raise KeyRefused("Give the key a name of at most 120 characters.")
    ops = sorted({str(o) for o in (operations or [])})
    if not ops or any(o not in OPERATIONS for o in ops):
        raise KeyRefused("Choose what the key may do: chat, read or manage.")
    if hubs is not None:
        hubs = sorted({str(h).strip().lower() for h in hubs if str(h).strip()})
        if not hubs:
            raise KeyRefused("Choose at least one agent, or allow all of them.")
        if ops != ["chat"]:
            # Reads and changes reach deployment-wide screens, which an agent
            # list can't limit; a key for some agents only chats with them.
            raise KeyRefused("A key limited to some agents can only chat with them.")
    if expires_in_days is not None:
        if not isinstance(expires_in_days, int) or not 1 <= expires_in_days <= MAX_LIFETIME_DAYS:
            raise KeyRefused(f"Choose an expiry of 1 to {MAX_LIFETIME_DAYS} days.")
    key_id = secrets.token_hex(8)
    secret = secrets.token_urlsafe(32)
    token = f"{PREFIX}{key_id}_{secret}"
    now = time.time()
    engine = engine_for(Path(hub_dir))
    with engine.begin() as conn:
        row = conn.execute(sa.select(users.c.auth_epoch, users.c.status).where(
            users.c.id == str(user.id))).first()
        if row is None or row[1] != "active":
            raise KeyRefused("This account can't make keys.")
        live = conn.execute(sa.select(sa.func.count()).select_from(api_keys).where(
            api_keys.c.user_id == str(user.id), api_keys.c.revoked_at.is_(None))).scalar()
        if int(live or 0) >= MAX_KEYS:
            raise KeyRefused(f"You already have {MAX_KEYS} keys. Revoke one first.")
        conn.execute(api_keys.insert().values(
            id=key_id, user_id=str(user.id), name=name, prefix=token[:12],
            token_hash=_digest(token), scopes=json.dumps(ops),
            hubs=json.dumps(hubs) if hubs is not None else None,
            auth_epoch=int(row[0] or 0), created_at=now,
            expires_at=now + expires_in_days * 86400 if expires_in_days else None,
            last_used_at=None, revoked_at=None))
        created = conn.execute(sa.select(api_keys).where(api_keys.c.id == key_id)).first()
    return _public(created), token


def list_for(hub_dir: Path, user_id: str) -> list[dict]:
    engine = engine_for(Path(hub_dir))
    with engine.connect() as conn:
        epoch = conn.execute(sa.select(users.c.auth_epoch).where(
            users.c.id == str(user_id))).scalar()
        rows = conn.execute(sa.select(api_keys).where(api_keys.c.user_id == str(user_id))
                            .order_by(api_keys.c.created_at.desc())).fetchall()
    now = time.time()
    out = []
    for row in rows:
        item = _public(row)
        m = row._mapping
        if item["revoked_at"] or int(m["auth_epoch"]) != int(epoch or 0):
            item["state"] = "revoked"
        elif item["expires_at"] and item["expires_at"] <= now:
            item["state"] = "expired"
        else:
            item["state"] = "active"
        out.append(item)
    return out


def revoke(hub_dir: Path, user_id: str, key_id: str) -> bool:
    if not _ID.match(str(key_id or "")):
        return False
    with engine_for(Path(hub_dir)).begin() as conn:
        return bool(conn.execute(api_keys.update().where(
            api_keys.c.id == str(key_id), api_keys.c.user_id == str(user_id),
            api_keys.c.revoked_at.is_(None)).values(revoked_at=time.time())).rowcount)


def resolve(hub_dir: Path, token: str) -> dict | None:
    """The key's owner and limits, when the key is live:
    ``{"key_id", "user": {...}, "operations", "hubs"}``. None otherwise."""
    if not looks_like_key(token):
        return None
    engine = engine_for(Path(hub_dir))
    with engine.connect() as conn:
        row = conn.execute(
            sa.select(api_keys, users.c.email, users.c.name.label("user_name"), users.c.role, users.c.status,
                      users.c.auth_epoch.label("current_epoch"))
            .select_from(api_keys.join(users, users.c.id == api_keys.c.user_id))
            .where(api_keys.c.token_hash == _digest(token))).first()
    if row is None:
        return None
    m = row._mapping
    now = time.time()
    if (m["revoked_at"] is not None or (m["expires_at"] and m["expires_at"] <= now)
            or m["status"] != "active" or int(m["current_epoch"] or 0) != int(m["auth_epoch"])):
        return None
    from .. import appmode
    from .users import is_local_address

    if is_local_address(m["email"]) and appmode.auth_enabled(Path(hub_dir)):
        # Made by whoever reached the server while sign-in was off, with no
        # proof of who they were: nobody can sign in as that owner to see or
        # revoke it, so it stops once sign-in is on.
        return None
    if not m["last_used_at"] or now - m["last_used_at"] > TOUCH_SECONDS:
        with engine.begin() as conn:
            conn.execute(api_keys.update().where(api_keys.c.id == m["id"])
                         .values(last_used_at=now))
    return {"key_id": m["id"], "operations": tuple(json.loads(m["scopes"] or "[]")),
            "hubs": tuple(json.loads(m["hubs"])) if m["hubs"] else None,
            "user": {"id": m["user_id"], "email": m["email"], "name": m["user_name"] or "",
                     "role": m["role"]}}


def _under(path: str, prefixes) -> bool:
    return any(path == p or path.startswith(p + "/") for p in prefixes)


def allows(operations, method: str, path: str) -> bool:
    """Whether a key with ``operations`` may make this request."""
    method = (method or "GET").upper()
    for never_method, prefix in _NEVER:
        if path.startswith(prefix) and (not never_method or never_method == method):
            return False
    if "/change-requests/" in path and path.endswith(("/confirm", "/reject")):
        return False
    ops = set(operations or ())
    if "chat" in ops:
        reads = method in ("GET", "HEAD")
        if reads and _under(path, _CHAT):
            return True
        if not reads and (path == "/api/chat" or _under(path, _CHAT_WRITES)):
            return True
    if method in ("GET", "HEAD") and "read" in ops:
        return True
    return "manage" in ops
