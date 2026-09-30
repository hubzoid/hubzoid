"""Each person's tokens for each connector (``hz_connector_tokens``).

One row per (person, connector). The token record is encrypted JSON with the
deployment key and carries everything needed to refresh or revoke it on its
own: the token endpoint, the resource, the client it was issued to. So a
connector whose dynamic client is registered again, or an administrator who
changes the client, never strands an existing connection.

Refresh (``access_token_for``) happens shortly before expiry and is safe when
several turns, threads or processes want the same token at once:

  * one refresh per (person, connector) per process (a lock), and
  * across processes a lease on the row (``refresh_lock_until``), taken by a
    compare-and-set on ``version``. Anyone else waits briefly for the new
    token instead of spending the same refresh token, which a rotating
    provider treats as replay and answers by revoking the whole grant.

A refresh the provider rejects (``invalid_grant``: revoked, expired, or a
replayed refresh token) marks the connection ``expired``: the person reconnects.
A provider that cannot be reached marks it ``error`` and the next turn retries.

Status: ``ok`` | ``expired`` | ``error``. Tokens, codes and secrets never reach
a log line.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from .. import secretbox
from ..access.identity import normalize
from . import ConnectorError, engine
from . import http as net

log = logging.getLogger("hubzoid.connectors")

SKEW = 60            # refresh this many seconds before the access token expires
LEASE_SECONDS = 30   # longer than one refresh request can take
WAIT_SECONDS = 10    # how long a turn waits for another process's refresh
_POLL = 0.2
REVOKE_TIMEOUT = 5.0

_COLUMNS = ("user_id", "connector_id", "email", "token_enc", "expires_at", "status", "error",
            "created_at", "updated_at", "version", "refresh_lock_until")
_META = ("user_id", "connector_id", "email", "expires_at", "status", "error", "created_at",
         "updated_at", "version")

_locks: dict[tuple, threading.Lock] = {}
_locks_guard = threading.Lock()


@dataclass(frozen=True)
class Connection:
    """A person's connection, without its secrets."""

    user_id: str
    connector_id: str
    email: str
    status: str
    error: str | None
    connected_at: float
    updated_at: float
    expires_at: float | None
    version: int


def _conn(row) -> Connection:
    d = dict(zip(_META, row))
    return Connection(user_id=d["user_id"], connector_id=d["connector_id"], email=d["email"],
                      status=d["status"] or "ok", error=d["error"],
                      connected_at=float(d["created_at"] or 0), updated_at=float(d["updated_at"] or 0),
                      expires_at=float(d["expires_at"]) if d["expires_at"] is not None else None,
                      version=int(d["version"] or 0))


# ---------------------------------------------------------------------------
# Who: the account a chat turn's identity (an email) belongs to
# ---------------------------------------------------------------------------
def user_id_for(hub_dir, email: str | None) -> str | None:
    """The account id tokens are stored under for ``email``, resolved the way
    sign-in resolves a request: the local owner when sign-in is off, else the
    active Hubzoid account with that email. None for anyone else, so a deleted
    account's connections never pass to a new account reusing the email."""
    from .. import appmode

    email = normalize(email)
    if not email:
        return None
    if not appmode.auth_enabled(Path(hub_dir)):
        from ..auth import local_owner

        owner = local_owner(Path(hub_dir))
        return owner.id if normalize(owner.email) == email else None
    with engine(hub_dir).connect() as conn:
        row = conn.execute(text("SELECT id, status FROM hz_users WHERE lower(email) = :e"),
                           {"e": email}).fetchone()
    if not row or (row[1] or "active") != "active":
        return None
    return str(row[0])


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
def store(hub_dir, *, user_id: str, email: str, connector_id: str, token: dict,
          now: float | None = None) -> None:
    """Save a new authorization (replacing any earlier one) as ``ok``."""
    now = time.time() if now is None else now
    enc = secretbox.encrypt_json(Path(hub_dir), token)
    exp = token.get("expires_at")
    params = {"u": user_id, "c": connector_id, "email": normalize(email), "t": enc,
              "e": float(exp) if exp else None, "now": now}
    eng = engine(hub_dir)
    for _ in range(3):
        with eng.begin() as conn:
            res = conn.execute(text(
                "UPDATE hz_connector_tokens SET email = :email, token_enc = :t, expires_at = :e, "
                "status = 'ok', error = NULL, created_at = :now, updated_at = :now, "
                "version = version + 1, refresh_lock_until = NULL "
                "WHERE user_id = :u AND connector_id = :c"), params)
            if res.rowcount == 1:
                return
        try:
            with eng.begin() as conn:
                conn.execute(text(
                    "INSERT INTO hz_connector_tokens (user_id, connector_id, email, token_enc, "
                    "expires_at, status, error, created_at, updated_at, version, "
                    "refresh_lock_until) VALUES (:u, :c, :email, :t, :e, 'ok', NULL, :now, :now, "
                    "0, NULL)"), params)
            return
        except IntegrityError:
            continue  # someone inserted first: update theirs
    raise ConnectorError("busy", "The connection could not be saved. Try again.", 409)


def get(hub_dir, user_id: str, connector_id: str) -> Connection | None:
    with engine(hub_dir).connect() as conn:
        r = conn.execute(text("SELECT " + ", ".join(_META) + " FROM hz_connector_tokens "
                              "WHERE user_id = :u AND connector_id = :c"),
                         {"u": user_id, "c": connector_id}).fetchone()
    return _conn(r) if r else None


def for_user(hub_dir, user_id: str) -> list[Connection]:
    if not user_id:
        return []
    with engine(hub_dir).connect() as conn:
        rows = conn.execute(text("SELECT " + ", ".join(_META) + " FROM hz_connector_tokens "
                                 "WHERE user_id = :u ORDER BY connector_id"),
                            {"u": user_id}).fetchall()
    return [_conn(r) for r in rows]


def counts(hub_dir) -> dict[str, int]:
    """Connections per connector (for the Console)."""
    with engine(hub_dir).connect() as conn:
        rows = conn.execute(text("SELECT connector_id, count(*) FROM hz_connector_tokens "
                                 "GROUP BY connector_id")).fetchall()
    return {r[0]: int(r[1]) for r in rows}


def _load(hub_dir, user_id: str, connector_id: str):
    """(row dict, decrypted token or None) or None when not connected."""
    with engine(hub_dir).connect() as conn:
        r = conn.execute(text("SELECT " + ", ".join(_COLUMNS) + " FROM hz_connector_tokens "
                              "WHERE user_id = :u AND connector_id = :c"),
                         {"u": user_id, "c": connector_id}).fetchone()
    if not r:
        return None
    row = dict(zip(_COLUMNS, r))
    try:
        token = secretbox.decrypt_json(Path(hub_dir), row["token_enc"])
    except (secretbox.SecretKeyError, ValueError):
        log.warning("connectors: %s's %s connection cannot be decrypted with the deployment "
                    "key", row["email"], connector_id)
        token = None
    return row, (token if isinstance(token, dict) else None)


def token_record(hub_dir, user_id: str, connector_id: str) -> dict | None:
    """The decrypted record (internal: revocation, tests). Never log it."""
    rec = _load(hub_dir, user_id, connector_id)
    return rec[1] if rec else None


# ---------------------------------------------------------------------------
# Fresh access tokens
# ---------------------------------------------------------------------------
def _due(token: dict, now: float) -> bool:
    exp = token.get("expires_at")
    if not exp:
        return False
    try:
        return now >= float(exp) - SKEW
    except (TypeError, ValueError):
        return True


def _usable(token: dict, now: float) -> bool:
    """Still valid for a few seconds, even if a refresh is due."""
    exp = token.get("expires_at")
    if not exp:
        return True
    try:
        return now < float(exp) - 5
    except (TypeError, ValueError):
        return False


def _lock_for(hub_dir, user_id: str, connector_id: str) -> threading.Lock:
    key = (str(engine(hub_dir).url), user_id, connector_id)
    with _locks_guard:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.Lock()
        return lock


def _matches(token: dict, url: str | None) -> bool:
    """A token is only ever sent to the server it was issued for."""
    return url is None or token.get("url") in (None, url)


def access_token_for(hub_dir, user_id: str, connector_id: str, *, url: str | None = None) -> str | None:
    """A currently valid access token for this person and connector, refreshed
    when it is about to expire. None when there is none to use now (not
    connected, expired, refused by the provider, or for a different ``url``).
    Never raises into a chat turn for a provider problem."""
    rec = _load(hub_dir, user_id, connector_id)
    if rec is None:
        return None
    row, token = rec
    if row["status"] == "expired" or token is None or not _matches(token, url):
        return None
    if not _due(token, time.time()):
        return token.get("access_token") or None
    with _lock_for(hub_dir, user_id, connector_id):
        return _refresh(hub_dir, user_id, connector_id, url=url)


async def access_token_for_async(hub_dir, user_id: str, connector_id: str, *,
                                 url: str | None = None) -> str | None:
    """``access_token_for`` without blocking the event loop. Concurrent
    coroutines share one refresh (the per-connection lock serializes them and
    the second finds the fresh token)."""
    import asyncio

    return await asyncio.to_thread(access_token_for, hub_dir, user_id, connector_id, url=url)


def _current(hub_dir, user_id, connector_id, url) -> str | None:
    rec = _load(hub_dir, user_id, connector_id)
    if rec is None:
        return None
    row, token = rec
    if row["status"] == "expired" or token is None or not _matches(token, url):
        return None
    return token.get("access_token") if _usable(token, time.time()) else None


def _take_lease(hub_dir, user_id, connector_id, version: int, now: float) -> bool:
    with engine(hub_dir).begin() as conn:
        res = conn.execute(text(
            "UPDATE hz_connector_tokens SET refresh_lock_until = :until "
            "WHERE user_id = :u AND connector_id = :c AND version = :v AND status <> 'expired' "
            "AND (refresh_lock_until IS NULL OR refresh_lock_until < :now)"),
            {"until": now + LEASE_SECONDS, "u": user_id, "c": connector_id, "v": version,
             "now": now})
    return res.rowcount == 1


def _finish(hub_dir, user_id, connector_id, version: int, *, status: str, error: str | None,
            token: dict | None = None, bump: bool = True) -> bool:
    """Compare-and-set the row written at ``version``; releases the lease."""
    sets = ["status = :status", "error = :error", "updated_at = :now", "refresh_lock_until = NULL"]
    params = {"status": status, "error": error, "now": time.time(), "u": user_id,
              "c": connector_id, "v": version}
    if bump:
        sets.append("version = version + 1")
    if token is not None:
        sets += ["token_enc = :t", "expires_at = :e"]
        params["t"] = secretbox.encrypt_json(Path(hub_dir), token)
        exp = token.get("expires_at")
        params["e"] = float(exp) if exp else None
    with engine(hub_dir).begin() as conn:
        res = conn.execute(text("UPDATE hz_connector_tokens SET " + ", ".join(sets)
                                + " WHERE user_id = :u AND connector_id = :c AND version = :v"),
                           params)
    return res.rowcount == 1


def _refresh(hub_dir, user_id: str, connector_id: str, *, url: str | None) -> str | None:
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        rec = _load(hub_dir, user_id, connector_id)
        if rec is None:
            return None
        row, token = rec
        if row["status"] == "expired" or token is None or not _matches(token, url):
            return None
        now = time.time()
        if not _due(token, now):
            return token.get("access_token") or None  # another thread or process refreshed it
        if not token.get("refresh_token") or not token.get("token_endpoint"):
            if _usable(token, now):
                return token.get("access_token") or None
            if _finish(hub_dir, user_id, connector_id, row["version"], status="expired",
                       error="no_refresh_token"):
                log.info("connectors: %s's %s connection expired and cannot be refreshed",
                         row["email"], connector_id)
            return None
        if _take_lease(hub_dir, user_id, connector_id, row["version"], now):
            break
        if time.monotonic() >= deadline:
            # Another process is still refreshing: use the current token while it lasts.
            return token.get("access_token") if _usable(token, now) else None
        time.sleep(_POLL)

    version = row["version"]
    outcome, result = _post_refresh(token)
    if outcome == "ok":
        now = time.time()
        fresh = dict(token)
        fresh["access_token"] = result.access_token
        fresh["token_type"] = result.token_type
        fresh["expires_at"] = now + int(result.expires_in) if result.expires_in else None
        if result.refresh_token:
            fresh["refresh_token"] = result.refresh_token  # a rotating provider
        if result.scope:
            fresh["scope"] = result.scope
        if _finish(hub_dir, user_id, connector_id, version, status="ok", error=None, token=fresh):
            log.info("connectors: refreshed %s's %s connection", row["email"], connector_id)
            return fresh["access_token"]
        # The person reconnected or disconnected meanwhile: their newer state
        # wins, and the tokens just issued are not kept anywhere.
        _revoke_later(fresh)
        return _current(hub_dir, user_id, connector_id, url)
    if outcome == "rejected":
        if result == "invalid_client":
            _forget_client(hub_dir, connector_id, token)
        if _finish(hub_dir, user_id, connector_id, version, status="expired",
                   error=f"refresh_rejected:{result}"):
            log.info("connectors: the provider refused to refresh %s's %s connection (%s); "
                     "they need to reconnect", row["email"], connector_id, result)
            return None
        return _current(hub_dir, user_id, connector_id, url)
    # Unreachable or answering errors: keep the connection, retry next time.
    _finish(hub_dir, user_id, connector_id, version, status="error",
            error=f"refresh_unavailable:{result}", bump=False)
    log.warning("connectors: could not refresh %s's %s connection (%s); will retry",
                row["email"], connector_id, result)
    return token.get("access_token") if _usable(token, time.time()) else None


def _post_refresh(token: dict):
    """("ok", OAuthToken) | ("rejected", error code) | ("unavailable", reason)."""
    from mcp.shared.auth import OAuthToken

    data = {"grant_type": "refresh_token", "refresh_token": token["refresh_token"]}
    if token.get("resource"):
        data["resource"] = token["resource"]
    data, headers = net.client_auth(token.get("client") or {}, data)
    try:
        with net.client() as c:
            status, body = net.post_json(c, token["token_endpoint"], data=data, headers=headers)
    except httpx.HTTPError as exc:
        return "unavailable", type(exc).__name__
    if status == 200 and body:
        try:
            return "ok", OAuthToken.model_validate(body)
        except ValidationError:
            return "unavailable", "invalid_response"
    if status in (400, 401):
        return "rejected", net.oauth_error(body)
    return "unavailable", f"http_{status}"


def _forget_client(hub_dir, connector_id: str, token: dict) -> None:
    client = token.get("client") or {}
    if client.get("source") == "dynamic" and token.get("redirect_uri"):
        try:
            from . import registry

            registry.forget_registration(hub_dir, connector_id, token["redirect_uri"])
        except Exception:  # noqa: BLE001 — best effort
            log.debug("connectors: could not drop a refused client", exc_info=True)


# ---------------------------------------------------------------------------
# Revocation (RFC 7009) and disconnection
# ---------------------------------------------------------------------------
def revoke(token: dict) -> bool:
    """Ask the provider to revoke this token record (refresh token first, which
    revokes the grant at most providers, then the access token). Best effort:
    True when the provider confirmed at least one."""
    endpoint = token.get("revocation_endpoint")
    if not endpoint:
        return False
    done = False
    pairs = [("refresh_token", token.get("refresh_token")), ("access_token", token.get("access_token"))]
    try:
        with net.client(timeout=httpx.Timeout(REVOKE_TIMEOUT, connect=REVOKE_TIMEOUT)) as c:
            for hint, value in pairs:
                if not value:
                    continue
                data, headers = net.client_auth(token.get("client") or {},
                                                {"token": value, "token_type_hint": hint})
                status, body = net.post_json(c, endpoint, data=data, headers=headers)
                if (status == 400 and "client_secret" not in data
                        and net.oauth_error(body) == "invalid_request"):
                    # Servers built on the MCP Python SDK (1.27) reject a
                    # revocation without a client_secret field, even from a
                    # public client. Retry once with the field present.
                    status, _ = net.post_json(c, endpoint, data={**data, "client_secret": ""},
                                              headers=headers)
                done = done or status == 200
    except httpx.HTTPError as exc:
        log.info("connectors: revocation request failed (%s)", type(exc).__name__)
    return done


def _revoke_later(*records: dict) -> None:
    records = tuple(r for r in records if r and r.get("revocation_endpoint"))
    if not records:
        return

    def run():
        for r in records:
            try:
                revoke(r)
            except Exception:  # noqa: BLE001 — best effort
                log.debug("connectors: background revocation failed", exc_info=True)

    threading.Thread(target=run, name="hubzoid-connector-revoke", daemon=True).start()


def disconnect(hub_dir, user_id: str, connector_id: str, *, revoke_tokens: bool = True) -> bool:
    """Revoke at the provider when it offers revocation (best effort), then
    delete the connection and any authorization in flight. True when a
    connection existed."""
    rec = _load(hub_dir, user_id, connector_id)
    if rec is not None and revoke_tokens and rec[1]:
        try:
            revoke(rec[1])
        except Exception:  # noqa: BLE001 — never block a disconnect on the provider
            log.debug("connectors: revocation failed", exc_info=True)
    with engine(hub_dir).begin() as conn:
        res = conn.execute(text("DELETE FROM hz_connector_tokens WHERE user_id = :u "
                                "AND connector_id = :c"), {"u": user_id, "c": connector_id})
        conn.execute(text("DELETE FROM hz_connector_flows WHERE user_id = :u AND connector_id = :c"),
                     {"u": user_id, "c": connector_id})
    return res.rowcount > 0


def drop_connector(hub_dir, connector_id: str, *, revoke_tokens: bool = True) -> int:
    """Delete every person's connection to ``connector_id`` (the connector was
    removed or now points elsewhere). Revocation runs in the background."""
    with engine(hub_dir).connect() as conn:
        rows = conn.execute(text("SELECT token_enc FROM hz_connector_tokens "
                                 "WHERE connector_id = :c"), {"c": connector_id}).fetchall()
    records = []
    for (enc,) in rows:
        try:
            value = secretbox.decrypt_json(Path(hub_dir), enc)
        except (secretbox.SecretKeyError, ValueError):
            continue
        if isinstance(value, dict):
            records.append(value)
    with engine(hub_dir).begin() as conn:
        res = conn.execute(text("DELETE FROM hz_connector_tokens WHERE connector_id = :c"),
                           {"c": connector_id})
        conn.execute(text("DELETE FROM hz_connector_flows WHERE connector_id = :c"),
                     {"c": connector_id})
    if revoke_tokens:
        _revoke_later(*records)
    return res.rowcount
