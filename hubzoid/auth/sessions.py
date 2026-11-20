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

A session is created only if the account has not changed since its
credential was read (``expect_updated_at``): a password reset, role or status
change racing a sign-in can't leave a session that outlives it.

Local mode (sign-in off): a request made on this machine is the local owner,
``admin@localhost``. Because that owner is implicit, a request is the owner only
when all of these hold (``local_request_allowed``):

* it names this server by ``localhost`` (or a ``.localhost`` name), a loopback
  address, or the address the server listens on, so a web page reaching it
  through DNS rebinding (its own host name) is refused;
* it carries no header a proxy or tunnel adds (``appmode.FORWARDING_HEADERS``),
  so a tunnel such as ngrok or Cloudflare Tunnel, whose local leg connects to
  loopback, does not make internet visitors the owner;
* the connection comes from this machine (the client address is loopback);
* an ``Origin``, when sent, is this machine too (another site's page, or a
  sandboxed page whose origin is ``null``, is not the owner).

A raw TCP forward (``ssh -R``) adds no header and can't be told apart from a
local visit: turn sign-in on before exposing the port that way.

``HUBZOID_ALLOW_UNAUTHENTICATED_NETWORK=true`` (the operator accepts that
anyone who can reach the web app is its owner, for a trusted LAN or Docker)
keeps the earlier, wider rule: any IP address or a configured origin's name.
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
TOUCH_INTERVAL = 300  # seconds between last_seen_at writes (less for short idle limits)
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


class SessionRace(Exception):
    """The account changed between reading its credential and starting the
    session (a password reset, a role or status change, a deletion)."""


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
    """Whether a request may act as the implicit local owner (see module doc).
    The listening address comes from the server socket (``scope["server"]``),
    which no request header can change."""
    if appmode.unauthenticated_network_allowed():
        return _network_request_allowed(request)
    host = _host_name(request)
    server = request.scope.get("server") or ()
    listening = server[0].strip("[]").lower() if server and isinstance(server[0], str) else ""
    peer = request.client.host if request.client is not None else None
    origin = (request.headers.get("origin") or "").strip()
    try:
        origin_host = (urlparse(origin).hostname or "") if origin else ""
    except ValueError:      # e.g. "http://[" : not a page of this machine
        origin_host = ""
    if host and not (appmode.is_loopback_name(host) or host == listening):
        key, why = "local:host", f"addressed to {host[:100]!r}"
    elif appmode.is_forwarded(request.headers):
        key, why = "local:proxy", "that came through a proxy or tunnel"
    elif peer is not None and not appmode.is_loopback_host(peer):
        key, why = "local:peer", f"from {peer[:64]!r}"
    elif origin and not (origin_host and (appmode.is_loopback_name(origin_host) or origin_host == host)):
        # `Origin: null` (a sandboxed page, an agent-written artifact) proves nothing local.
        key, why = "local:origin", f"from the page {origin[:100]!r}"
    else:
        return True
    # Once per kind: a stream of internet visitors must not grow this set.
    if key not in _warned_hosts:
        _warned_hosts.add(key)
        log.warning("auth: sign-in is off, so only this computer is the owner; a request %s was not "
                    "treated as the owner. To use Hubzoid from another address, turn sign-in on "
                    "(HUBZOID_AUTH=true).", why)
    return False


def _network_request_allowed(request: Request) -> bool:
    """The wider rule under HUBZOID_ALLOW_UNAUTHENTICATED_NETWORK: the Host is
    ``localhost``, an IP address, the listening address or a configured
    origin's name. Only a page using DNS rebinding (its own name) is refused."""
    host = _host_name(request)
    if not host or host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    server = request.scope.get("server") or ()
    if server and isinstance(server[0], str) and host == server[0].strip("[]").lower():
        return True
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
                             name=user.get("name") or "Local owner", role="admin", method="local",
                             credential="local")
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


def bearer_token(request: Request) -> str:
    """A personal API key sent as ``Authorization: Bearer hzp_...``, else ""."""
    raw = request.headers.get("authorization") or ""
    if not raw.lower().startswith("bearer "):
        return ""
    token = raw[7:].strip()
    # Anything that claims to be a personal key is judged as one: a malformed
    # or overlong ``hzp_`` value is refused, never answered by the cookie or
    # the local owner instead.
    return token if token.startswith("hzp_") else ""


def _key_user(request: Request, hub_dir: Path, token: str) -> AuthUser | None:
    """The person behind a personal API key, within its limits. A key the
    request may not use answers 403 (never the cookie, never the local owner)."""
    from . import api_keys

    if request.cookies.get(SESSION_COOKIE):
        raise HTTPException(status_code=400, detail={
            "code": "ambiguous_credentials",
            "message": "Send either a session cookie or an API key, not both."})
    try:
        info = api_keys.resolve(Path(hub_dir), token)
        if info is None:
            return None
        from ..access import store_for

        if store_for(Path(hub_dir)).is_suspended(info["user"]["email"]):
            return None
    except (SQLAlchemyError, OSError, ValueError, RuntimeError):
        log.warning("auth: API key check failed (store unavailable)", exc_info=True)
        raise HTTPException(status_code=503, detail=dict(_UNAVAILABLE))
    path = request.url.path
    if not api_keys.allows(info["operations"], request.method, path):
        raise HTTPException(status_code=403, detail={
            "code": "key_not_allowed",
            "message": "This API key can't do this. Use a key with more access, or sign in."})
    hubs = info["hubs"]
    if hubs is not None and path not in api_keys.LIMITED_PATHS:
        raise HTTPException(status_code=403, detail={
            "code": "key_not_allowed",
            "message": "A key limited to some agents works with the chat API (/v1) only."})
    if hubs is not None and any(path.startswith(p) for p in api_keys.HUB_PATHS):
        from ..access.identity import normalize

        if normalize(Path(hub_dir).name) not in hubs:
            raise HTTPException(status_code=403, detail={
                "code": "key_not_allowed", "message": "This API key can't use this agent."})
    u = info["user"]
    return AuthUser(id=u["id"], email=u["email"], name=u["name"], role=u["role"],
                    method="api_key", credential="api_key", credential_id=info["key_id"],
                    operations=info["operations"], hubs=hubs)


def _unknown_jwt_bearer(request: Request) -> bool:
    raw = request.headers.get("authorization") or ""
    if not raw.lower().startswith("bearer "):
        return False
    token = raw[7:].strip()
    return not token.startswith("hzp_") and token.count(".") == 2


def app_bearer(request: Request, hub_dir: Path) -> str:
    """A machine JWT (``Authorization: Bearer <jwt>``) from a registered API
    app (``apps:`` with an ``api:`` block), else "". Only routes the
    credential: ``_app_user`` verifies it."""
    raw = request.headers.get("authorization") or ""
    if not raw.lower().startswith("bearer "):
        return ""
    token = raw[7:].strip()
    if token.startswith("hzp_") or token.count(".") != 2:
        return ""
    from ..embed import tokens

    return token if tokens.api_app(Path(hub_dir), token) is not None else ""


def _app_user(request: Request, hub_dir: Path, token: str) -> AuthUser:
    """The account an API app's JWT acts as, within the app's operations. Like
    a key, it never falls back to the cookie or the local owner, and a
    refusal is final."""
    from ..embed import tokens
    from ..embed.config import AppsError
    from . import api_keys

    if request.cookies.get(SESSION_COOKIE):
        raise HTTPException(status_code=400, detail={
            "code": "ambiguous_credentials",
            "message": "Send either a session cookie or a token, not both."})
    try:
        app, _claims = tokens.api_caller(Path(hub_dir), token)
    except AppsError as exc:
        status = exc.status if exc.status in (401, 403) else 503
        raise HTTPException(status_code=status, detail={"code": exc.code, "message": exc.message})
    try:
        from ..access import store_for
        from . import users as userlib

        account = userlib.find_by_email(Path(hub_dir), app.api.account)
        suspended = account is not None and store_for(Path(hub_dir)).is_suspended(account["email"])
    except (SQLAlchemyError, OSError, RuntimeError):
        log.warning("auth: app token check failed (store unavailable)", exc_info=True)
        raise HTTPException(status_code=503, detail=dict(_UNAVAILABLE))
    if account is None or account.get("status") != "active" or suspended:
        raise HTTPException(status_code=403, detail={
            "code": "account_unavailable",
            "message": f"The account this app acts as ({app.api.account}) can't be used. Ask an administrator."})
    if not api_keys.allows(app.api.operations, request.method, request.url.path):
        raise HTTPException(status_code=403, detail={
            "code": "key_not_allowed",
            "message": "This app's tokens can't do this. An administrator can add operations to its api block."})
    return AuthUser(id=account["id"], email=account["email"], name=account["name"], role=account["role"],
                    method="app_jwt", credential="app_jwt", credential_id=f"app:{app.id}",
                    operations=app.api.operations, hubs=None)


def resolve(request: Request, hub_dir: Path) -> AuthUser | None:
    """Who is making this request, or None. A personal API key (bearer) is
    checked first, then a machine JWT from an API app; neither ever falls back
    to the cookie or the local owner.
    Local mode: the local owner."""
    hub_dir = Path(hub_dir)
    hit = _cached(request, hub_dir)
    if hit is not None:
        return hit[1]
    token = bearer_token(request)
    if token:
        user = _key_user(request, hub_dir, token)
        _remember(request, hub_dir, user)
        return user
    machine = app_bearer(request, hub_dir)
    if machine:
        user = _app_user(request, hub_dir, machine)
        _remember(request, hub_dir, user)
        return user
    if _unknown_jwt_bearer(request):
        # A JWT-shaped bearer that is no API app's (an embedding pass, an MCP
        # token, a stranger's token) is never the cookie's person or the
        # local owner: those routes check it themselves, or refuse it.
        _remember(request, hub_dir, None)
        return None
    if not appmode.auth_enabled(hub_dir):
        user = local_owner(hub_dir) if local_request_allowed(request) else None
    else:
        token = request.cookies.get(SESSION_COOKIE) or ""
        user = resolve_token(hub_dir, token) if token else None
    _remember(request, hub_dir, user)
    return user


def ended_by_block(hub_dir: Path, token: str) -> bool:
    """Whether this (no longer valid) session belongs to a blocked account, so
    sign-in can say why it ended (HUB-140 F23)."""
    if not token or len(token) > 256:
        return False
    try:
        with engine_for(Path(hub_dir)).connect() as conn:
            email = conn.execute(
                sa.select(users.c.email).select_from(sessions.join(users, users.c.id == sessions.c.user_id))
                .where(sessions.c.token_hash == digest(token))).scalar()
        if not email:
            return False
        from ..access import store_for

        return store_for(Path(hub_dir)).is_suspended(email)
    except (SQLAlchemyError, OSError, ValueError, RuntimeError):
        return False


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
            # Blocked: this session ends for good, even if the block is lifted.
            with engine.begin() as conn:
                conn.execute(sessions.update().where(
                    sessions.c.token_hash == hashed, sessions.c.revoked_at.is_(None),
                ).values(revoked_at=now))
            return None
        if now - m["last_seen_at"] >= min(TOUCH_INTERVAL, idle / 2):
            with engine.begin() as conn:
                conn.execute(sessions.update().where(
                    sessions.c.token_hash == hashed, sessions.c.revoked_at.is_(None),
                ).values(last_seen_at=now))
        return AuthUser(id=m["user_id"], email=m["email"], name=m["name"] or "",
                        role=m["role"], method=m["method"] or "",
                        credential_id=session_id(hashed))
    except (SQLAlchemyError, OSError, ValueError, RuntimeError):
        # The account or access store can't be read: nobody is signed in on
        # its say-so (fail closed), and the caller hears it is unavailable.
        log.warning("auth: session check failed (store unavailable)", exc_info=True)
        raise HTTPException(status_code=503, detail=dict(_UNAVAILABLE))


# ---- creation and revocation --------------------------------------------------

MAX_SESSIONS = 10


def create_session(hub_dir: Path, user, *, method: str, request: Request | None = None,
                   expect_updated_at: float | None = None, mfa: bool = False) -> str:
    """Start a session for ``user`` (an AuthUser or an account dict). Returns
    the token for the cookie; only its digest is stored.

    ``expect_updated_at`` is the account's ``updated_at`` when its credential
    was read. The session row is written first, then the account is read
    again under a lock (``FOR SHARE`` on PostgreSQL; SQLite already holds its
    write lock): if the account changed meanwhile, or is no longer active,
    nothing is kept and SessionRace is raised. A change that commits later
    revokes the new session like any other."""
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
            revoked_at=None, mfa_at=now if mfa else None,
        ))
        if expect_updated_at is not None:
            current = conn.execute(
                sa.select(users.c.updated_at, users.c.status)
                .where(users.c.id == str(user_id)).with_for_update(read=True)
            ).first()
            if current is None or current[1] != "active" or current[0] != expect_updated_at:
                raise SessionRace()
        # This person's long-finished sessions are no longer useful.
        conn.execute(sessions.delete().where(
            sessions.c.user_id == str(user_id),
            sa.or_(sessions.c.expires_at < now - 86400, sessions.c.revoked_at < now - 86400),
        ))
        # At most MAX_SESSIONS signed-in devices: the least recently used go.
        live = conn.execute(sa.select(sessions.c.token_hash).where(
            sessions.c.user_id == str(user_id), sessions.c.revoked_at.is_(None),
            sessions.c.expires_at > now,
        ).order_by(sessions.c.last_seen_at.desc()).offset(MAX_SESSIONS)).fetchall()
        if live:
            conn.execute(sessions.update().where(
                sessions.c.token_hash.in_([r[0] for r in live])).values(revoked_at=now))
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


# ---- the signed-in person's devices ----------------------------------------------

RECENT_SECONDS = 600


def session_id(token_hash: str) -> str:
    """A public id for a session: the start of its digest (the token can't be
    derived from it)."""
    return token_hash[:16]


def _device(agent: str | None) -> str:
    """'Chrome on macOS' from a user agent, for people reading their devices."""
    ua = agent or ""
    browser = next((name for key, name in (("Edg/", "Edge"), ("OPR/", "Opera"),
                                           ("Firefox/", "Firefox"), ("Chrome/", "Chrome"),
                                           ("Safari/", "Safari"), ("curl/", "curl"),
                                           ("python", "Python"), ("hubzoid", "Hubzoid CLI"))
                    if key.lower() in ua.lower()), "Browser")
    system = next((name for key, name in (("iPhone", "iPhone"), ("iPad", "iPad"),
                                          ("Android", "Android"), ("Mac OS X", "macOS"),
                                          ("Macintosh", "macOS"), ("Windows", "Windows"),
                                          ("Linux", "Linux")) if key in ua), "")
    return f"{browser} on {system}" if system else browser


def _place(ip: str | None) -> str:
    """The address, shortened: enough to recognise a network, not a person."""
    if not ip:
        return ""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return ""
    if addr.is_loopback:
        return "This computer"
    if addr.version == 4:
        parts = ip.split(".")
        return ".".join(parts[:3]) + ".x"
    return ip.split(":")[0] + ":…"


def list_sessions(hub_dir: Path, user_id: str, current: str | None = None) -> list[dict]:
    now = time.time()
    current_hash = digest(current) if current else ""
    with engine_for(Path(hub_dir)).connect() as conn:
        rows = conn.execute(sa.select(sessions).where(
            sessions.c.user_id == str(user_id), sessions.c.revoked_at.is_(None),
            sessions.c.expires_at > now,
        ).order_by(sessions.c.last_seen_at.desc())).fetchall()
    out = []
    for row in rows:
        m = row._mapping
        if m["last_seen_at"] + min(m["idle_seconds"], idle_seconds()) <= now:
            continue
        out.append({"id": session_id(m["token_hash"]), "device": _device(m["user_agent"]),
                    "place": _place(m["ip"]), "signed_in_at": m["created_at"],
                    "last_active_at": m["last_seen_at"], "method": m["method"] or "",
                    "current": m["token_hash"] == current_hash})
    return out


def revoke_session_id(hub_dir: Path, user_id: str, sid: str) -> bool:
    if not sid or len(sid) != 16 or not all(c in "0123456789abcdef" for c in sid):
        return False
    with engine_for(Path(hub_dir)).begin() as conn:
        return bool(conn.execute(sessions.update().where(
            sessions.c.user_id == str(user_id), sessions.c.token_hash.like(sid + "%"),
            sessions.c.revoked_at.is_(None)).values(revoked_at=time.time())).rowcount)


def recently_verified(request: Request, hub_dir: Path) -> bool:
    """Whether this browser session proved who it is in the last ten minutes
    (signed in, or confirmed with ``/api/auth/reauth``). A personal API key
    never counts. Local mode: the local owner at this computer counts."""
    if bearer_token(request):
        return False
    if not appmode.auth_enabled(hub_dir):
        return True
    token = current_token(request)
    if not token:
        return False
    with engine_for(Path(hub_dir)).connect() as conn:
        row = conn.execute(sa.select(sessions.c.created_at, sessions.c.mfa_at).where(
            sessions.c.token_hash == digest(token), sessions.c.revoked_at.is_(None))).first()
    if row is None:
        return False
    latest = max(row[0] or 0, row[1] or 0)
    return time.time() - latest <= RECENT_SECONDS


def mark_verified(hub_dir: Path, request: Request) -> None:
    token = current_token(request)
    if not token:
        return
    with engine_for(Path(hub_dir)).begin() as conn:
        conn.execute(sessions.update().where(sessions.c.token_hash == digest(token))
                     .values(mfa_at=time.time()))
