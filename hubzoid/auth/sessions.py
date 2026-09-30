"""Sessions: who is signed in, from the ``hz_session`` cookie.

A session token is 32 random bytes (URL-safe base64). Only its SHA-256 digest
is stored (``hz_sessions.token_hash``), so the database never holds a usable
credential. The cookie is HttpOnly, SameSite=Lax, Path=/, and Secure when the
request's scheme is https (``X-Forwarded-Proto`` from the edge or a TLS proxy
counts).

A session is valid while all of these hold: it is not revoked, it is younger
than ``HUBZOID_SESSION_DAYS`` (30), it was used within the last
``HUBZOID_SESSION_IDLE_DAYS`` (7), its account exists and is ``active``, and
the account is not suspended in the access store. Lowering either setting
applies to existing sessions too. ``last_seen_at`` is written at most every
five minutes.

Local mode (sign-in off): every request is the local owner, ``admin@localhost``.
Because that owner is implicit, a request is only treated as the local owner
when it names this server by a loopback name, an IP address, a single-label
host name or an allowed origin, which stops a web page from reaching it
through DNS rebinding.
"""
from __future__ import annotations

import hashlib
import ipaddress
import logging
import os
import secrets
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import sqlalchemy as sa
from fastapi import HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError

from .. import appmode
from . import AuthUser
from .schema import attempts, engine_for, links, sessions, users

log = logging.getLogger("hubzoid.auth")

SESSION_COOKIE = "hz_session"
TOUCH_INTERVAL = 300  # seconds between last_seen_at writes
_SWEEP_INTERVAL = 3600
_UNAVAILABLE = {"code": "accounts_unavailable",
                "message": "Sign-in is unavailable right now. Try again shortly."}

_owner_cache: dict[tuple, AuthUser] = {}
_owner_lock = threading.Lock()
_sweeps: dict[int, float] = {}
_warned_hosts: set[str] = set()


def _days(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name) or default)
    except ValueError:
        return default
    return value if value > 0 else default


def lifetime_seconds() -> int:
    return int(_days("HUBZOID_SESSION_DAYS", 30) * 86400)


def idle_seconds() -> int:
    return int(_days("HUBZOID_SESSION_IDLE_DAYS", 7) * 86400)


def digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


# ---- request facts ------------------------------------------------------------

def is_https(request: Request | None) -> bool:
    """The scheme the browser used: ``X-Forwarded-Proto`` when present (the
    edge asserts it from the public URL; a TLS proxy sets it), else the URL."""
    if request is None:
        return False
    forwarded = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    if forwarded in ("http", "https"):
        return forwarded == "https"
    return request.url.scheme == "https"


def client_ip(request: Request | None) -> str:
    """The caller's address as the server sees it. Under uvicorn the bridge
    trusts ``X-Forwarded-For`` from its loopback peer (the edge)."""
    if request is None or request.client is None:
        return ""
    return (request.client.host or "")[:64]


def _host_name(request: Request) -> str:
    raw = (request.headers.get("host") or "").strip()
    if not raw:
        return ""
    try:
        return (urlparse("//" + raw).hostname or "").lower()
    except ValueError:
        return raw.lower()


def local_request_allowed(request: Request) -> bool:
    """Whether a request may act as the implicit local owner (see module doc)."""
    host = _host_name(request)
    if not host or host == "localhost" or host.endswith(".localhost") or "." not in host:
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    allowed = {(urlparse(o).hostname or "").lower() for o in appmode.allowed_origins()}
    if host in allowed:
        return True
    if host not in _warned_hosts:
        _warned_hosts.add(host)
        log.warning("auth: sign-in is off, so a request addressed to %r was not treated as the "
                    "local owner. Open this server by localhost or its IP address, or list the "
                    "name in HUBZOID_PUBLIC_URL or HUBZOID_ALLOWED_ORIGINS.", host[:100])
    return False


# ---- the local owner ------------------------------------------------------------

def _owner_key(hub_dir: Path) -> tuple:
    env = os.environ
    return (str(Path(hub_dir).resolve()), env.get("HUBZOID_OPERATIONAL_DB"),
            env.get("DATABASE_URL"), env.get("HUBZOID_DEPLOYMENT"))


def local_owner(hub_dir: Path) -> AuthUser:
    """The implicit owner when sign-in is off: ``admin@localhost``, an
    administrator. Its account row is created on first use (cached per process)."""
    key = _owner_key(hub_dir)
    found = _owner_cache.get(key)
    if found is not None:
        return found
    with _owner_lock:
        found = _owner_cache.get(key)
        if found is None:
            from .users import ensure_local_owner

            user = ensure_local_owner(Path(hub_dir))
            found = AuthUser(id=user["id"], email=user["email"],
                             name=user.get("name") or "Local owner", role="admin", method="local")
            _owner_cache[key] = found
    return found


def reset_cache() -> None:
    """Forget cached local owners (tests, and after renaming the owner)."""
    with _owner_lock:
        _owner_cache.clear()


# ---- resolution ---------------------------------------------------------------

_STATE_KEY = "_hz_auth"


def _cached(request: Request, hub_dir: Path):
    try:
        entry = getattr(request.state, _STATE_KEY, None)
    except Exception:  # noqa: BLE001 — a scope without state caches nothing
        return None
    if entry and entry[0] == str(hub_dir):
        return entry
    return None


def _remember(request: Request, hub_dir: Path, user: AuthUser | None) -> None:
    try:
        setattr(request.state, _STATE_KEY, (str(hub_dir), user))
    except Exception:  # noqa: BLE001
        pass


def forget(request: Request) -> None:
    """Drop this request's cached answer (after signing in or out)."""
    try:
        if hasattr(request.state, _STATE_KEY):
            delattr(request.state, _STATE_KEY)
    except Exception:  # noqa: BLE001
        pass


def resolve(request: Request, hub_dir: Path) -> AuthUser | None:
    """Who is making this request, or None. Local mode: the local owner."""
    hub_dir = Path(hub_dir)
    hit = _cached(request, hub_dir)
    if hit is not None:
        return hit[1]
    if not appmode.auth_enabled(hub_dir):
        user = local_owner(hub_dir) if local_request_allowed(request) else None
    else:
        token = request.cookies.get(SESSION_COOKIE) or ""
        user = resolve_token(hub_dir, token) if token else None
    _remember(request, hub_dir, user)
    return user


def resolve_token(hub_dir: Path, token: str) -> AuthUser | None:
    """The account behind a session token, when the session is valid."""
    if not token or len(token) > 256:
        return None
    hashed = digest(token)
    try:
        engine = engine_for(Path(hub_dir))
        with engine.connect() as conn:
            row = conn.execute(
                sa.select(sessions.c.user_id, sessions.c.created_at, sessions.c.last_seen_at,
                          sessions.c.expires_at, sessions.c.idle_seconds, sessions.c.method,
                          sessions.c.revoked_at, users.c.email, users.c.name, users.c.role,
                          users.c.status)
                .select_from(sessions.join(users, users.c.id == sessions.c.user_id))
                .where(sessions.c.token_hash == hashed)
            ).first()
        if row is None:
            return None
        m = row._mapping
        now = time.time()
        expires = min(m["expires_at"], m["created_at"] + lifetime_seconds())
        idle = min(m["idle_seconds"], idle_seconds())
        if m["revoked_at"] is not None or expires <= now or m["last_seen_at"] + idle <= now:
            return None
        if m["status"] != "active":
            return None
        from ..access import store_for

        if store_for(Path(hub_dir)).is_suspended(m["email"]):
            return None
        if now - m["last_seen_at"] >= TOUCH_INTERVAL:
            with engine.begin() as conn:
                conn.execute(sessions.update().where(
                    sessions.c.token_hash == hashed, sessions.c.revoked_at.is_(None),
                ).values(last_seen_at=now))
        return AuthUser(id=m["user_id"], email=m["email"], name=m["name"] or "",
                        role=m["role"], method=m["method"] or "")
    except SQLAlchemyError:
        log.warning("auth: session check failed (database error)")
        raise HTTPException(status_code=503, detail=dict(_UNAVAILABLE))


# ---- creation and revocation --------------------------------------------------

def create_session(hub_dir: Path, user, *, method: str, request: Request | None = None) -> str:
    """Start a session for ``user`` (an AuthUser or an account dict). Returns
    the token for the cookie; only its digest is stored."""
    user_id = user.id if isinstance(user, AuthUser) else user["id"]
    token = new_token()
    now = time.time()
    agent = (request.headers.get("user-agent") or "")[:512] if request is not None else ""
    engine = engine_for(Path(hub_dir))
    with engine.begin() as conn:
        conn.execute(sessions.insert().values(
            token_hash=digest(token), user_id=str(user_id), created_at=now, last_seen_at=now,
            expires_at=now + lifetime_seconds(), idle_seconds=idle_seconds(),
            user_agent=agent or None, ip=client_ip(request) or None, method=(method or "")[:16],
            revoked_at=None,
        ))
        # This person's long-finished sessions are no longer useful.
        conn.execute(sessions.delete().where(
            sessions.c.user_id == str(user_id),
            sa.or_(sessions.c.expires_at < now - 86400, sessions.c.revoked_at < now - 86400),
        ))
    _sweep(engine, now)
    return token


def _sweep(engine, now: float) -> None:
    """Now and then, delete long-finished sessions, links and attempt rows."""
    key = id(engine)
    if now - _sweeps.get(key, 0.0) < _SWEEP_INTERVAL:
        return
    _sweeps[key] = now
    cutoff = now - 7 * 86400
    try:
        with engine.begin() as conn:
            conn.execute(sessions.delete().where(
                sa.or_(sessions.c.expires_at < cutoff, sessions.c.revoked_at < cutoff)))
            conn.execute(links.delete().where(
                sa.or_(links.c.expires_at < cutoff, links.c.used_at < cutoff)))
            conn.execute(attempts.delete().where(
                attempts.c.window_start < cutoff,
                sa.or_(attempts.c.locked_until.is_(None), attempts.c.locked_until < now)))
    except SQLAlchemyError:
        log.warning("auth: clean-up of finished sessions failed; will retry later")


def revoke(hub_dir: Path, token: str | None) -> bool:
    """End the session with this token. Returns whether one was ended."""
    if not token:
        return False
    with engine_for(Path(hub_dir)).begin() as conn:
        return bool(conn.execute(sessions.update().where(
            sessions.c.token_hash == digest(token), sessions.c.revoked_at.is_(None),
        ).values(revoked_at=time.time())).rowcount)


def revoke_user(hub_dir: Path, user_id: str, except_token: str | None = None) -> int:
    """End every session of an account, except the one holding ``except_token``."""
    from .users import store

    return store(Path(hub_dir)).revoke_sessions(
        user_id, except_token_hash=digest(except_token) if except_token else None)


def current_token(request: Request) -> str:
    return request.cookies.get(SESSION_COOKIE) or ""


# ---- cookies ------------------------------------------------------------------

def set_cookie(response, token: str, request: Request | None = None) -> None:
    response.set_cookie(
        SESSION_COOKIE, token, max_age=lifetime_seconds(), path="/", httponly=True,
        samesite="lax", secure=is_https(request),
    )


def clear_cookie(response, request: Request | None = None) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="lax",
                           secure=is_https(request))
