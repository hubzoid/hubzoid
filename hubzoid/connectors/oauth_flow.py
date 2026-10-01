"""The browser authorization for a personal connection, in two halves that
survive restarts and work across bridges (state lives in the shared store).

``start(connector, user)``
    Discovery (``discovery.py``), then the client: the administrator's
    pre-registered one, or a client registered dynamically (RFC 7591) and cached
    encrypted on the connector for everyone. PKCE S256, the RFC 8707
    ``resource`` indicator when the server publishes resource metadata, the
    scopes, and a random ``state``. The flow row (``hz_connector_flows``) keeps
    only the SHA-256 of the state; the verifier, redirect URI, expected issuer
    and client travel in an encrypted payload. Ten minutes to finish.

``callback(state, code, iss)``
    Atomically consumes the flow (single use, replay refused), then checks it
    belongs to the signed-in person and connector and has not expired, checks
    ``iss`` (RFC 9207) against the issuer discovered at start (required when the
    server advertises it, compared whenever present), exchanges the code with
    the verifier and stores the tokens encrypted (``tokens.store``).

The redirect URI is ``{origin}/oauth/connectors/{id}/callback``. The ``mcp``
SDK provides the models, PKCE generation and discovery helpers; its
``OAuthClientProvider`` runs one interactive flow in one process, which is not
what a multi-user web server needs, so the rest is here.
"""
from __future__ import annotations

import copy
import hashlib
import logging
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from pydantic import ValidationError
from sqlalchemy import text

from .. import secretbox
from ..access.identity import normalize
from . import ConnectorError, engine, registry, tokens
from . import http as net
from .discovery import Discovery, discover

log = logging.getLogger("hubzoid.connectors")

FLOW_SECONDS = 600
MAX_OPEN_FLOWS = 20  # per person: older unfinished authorizations are dropped
CALLBACK_PATH = "/oauth/connectors/{id}/callback"
DISCOVERY_SECONDS = 60  # metadata reused for repeated connects (never secrets)
_AUTH_ORDER = ("none", "client_secret_basic", "client_secret_post")
_discovered: dict[str, tuple[float, Discovery]] = {}
_discovered_lock = threading.Lock()


class CallbackError(ConnectorError):
    """A callback that cannot complete. Carries where the browser goes next."""

    def __init__(self, code: str, message: str, *, connector_id: str | None = None,
                 return_to: str | None = None, journey_id: str | None = None,
                 email: str | None = None):
        super().__init__(code, message, 400)
        self.connector_id = connector_id
        self.return_to = return_to
        self.journey_id = journey_id
        self.email = email


@dataclass(frozen=True)
class Completed:
    connector_id: str
    return_to: str | None
    journey_id: str | None


# ---------------------------------------------------------------------------
# Small rules
# ---------------------------------------------------------------------------
def redirect_uri(origin: str, connector_id: str) -> str:
    return origin.rstrip("/") + CALLBACK_PATH.format(id=connector_id)


def request_origin(request) -> str:
    """The origin the browser used, as the edge forwards it (Host and
    X-Forwarded-Proto)."""
    from .. import appmode

    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    scheme = proto if proto in ("http", "https") else request.url.scheme
    host = request.headers.get("host") or request.url.netloc
    return appmode.normalize_origin(f"{scheme}://{host}")


def origin_for(request, *, strict: bool = True) -> str:
    """The origin redirect URIs are built on: the request's own origin when it
    is one this deployment serves (``HUBZOID_PUBLIC_URL`` and
    ``HUBZOID_ALLOWED_ORIGINS``), else the primary public origin.

    With nothing configured, only a loopback origin is trusted: a Host header
    is chosen by whoever sends the request, so a redirect URI is never built on
    another one. ``strict=False`` answers anyway (for display)."""
    from .. import appmode

    own = request_origin(request)
    allowed = appmode.allowed_origins()
    if allowed:
        return own if own in allowed else allowed[0]
    if strict and not net.is_loopback_url(own):
        raise ConnectorError("public_url_required", "Set HUBZOID_PUBLIC_URL to the address "
                             "people use for Hubzoid, then try again.", 409)
    return own


def safe_return_to(value) -> str | None:
    """A same-origin relative path (``/...``, not ``//``), or None."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 1024 or not value.startswith("/") or value.startswith("//"):
        return None
    if "\\" in value or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return None
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return None
    return value


def with_params(url: str, params: dict) -> str:
    """``url`` with ``params`` set in its query, keeping any other parameters."""
    parts = urlsplit(url)
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in params]
    query = urlencode(kept + [(k, v) for k, v in params.items() if v is not None])
    return urlunsplit(parts._replace(query=query))


def _digest(state: str) -> str:
    return hashlib.sha256(state.encode()).hexdigest()


# ---------------------------------------------------------------------------
# The client Hubzoid uses with the authorization server
# ---------------------------------------------------------------------------
def _pre_registered_method(supported: list[str] | None, has_secret: bool) -> str:
    if not has_secret:
        return "none"
    if supported is None or "client_secret_basic" in supported:
        return "client_secret_basic"  # RFC 8414 default
    if "client_secret_post" in supported:
        return "client_secret_post"
    if "none" in supported:
        return "none"
    raise ConnectorError("client_auth_unsupported", "The authorization server accepts none of "
                         "the client authentication methods Hubzoid supports.", 502)


def _register(c: httpx.Client, disc: Discovery, redirect: str, scope: str | None) -> dict:
    from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata

    supported = disc.token_auth_methods
    methods = [m for m in _AUTH_ORDER if supported is None or m in supported] or ["none"]
    failure = "unknown"
    for method in methods:
        meta = OAuthClientMetadata(
            redirect_uris=[redirect], token_endpoint_auth_method=method,
            grant_types=["authorization_code", "refresh_token"], response_types=["code"],
            scope=scope, client_name="Hubzoid", software_id="hubzoid")
        body = meta.model_dump(by_alias=True, mode="json", exclude_none=True)
        try:
            status, resp = net.post_json(c, disc.registration_endpoint, json_body=body)
        except httpx.HTTPError as exc:
            host = net.refused_host(exc)
            if host is not None:
                raise ConnectorError("private_address", net.refusal(
                    "The registration endpoint", host), 502) from None
            raise ConnectorError("unreachable", "The authorization server could not be reached "
                                 "to register Hubzoid.", 502) from exc
        if status in (200, 201) and resp:
            try:
                info = OAuthClientInformationFull.model_validate(resp)
            except ValidationError:
                failure = "invalid_response"
                break
            used = info.token_endpoint_auth_method or method
            if not info.client_id or (used != "none" and not info.client_secret):
                failure = "invalid_response"
                break
            return {"client_id": info.client_id, "client_secret": info.client_secret,
                    "auth_method": used, "source": "dynamic", "issuer": disc.issuer,
                    "scope": scope, "redirect_uri": redirect,
                    "client_secret_expires_at": info.client_secret_expires_at or 0,
                    "registered_at": time.time()}
        failure = net.oauth_error(resp) if resp else f"http_{status}"
        if status != 400:
            break
    log.warning("connectors: dynamic client registration for %s failed (%s)",
                disc.issuer, failure)
    raise ConnectorError("registration_failed", "The server refused to register Hubzoid as a "
                         f"client ({failure}). An administrator can register Hubzoid with the "
                         "provider and enter its client ID on the connector.", 502)


def client_for(hub_dir, connector: registry.Connector, disc: Discovery, redirect: str,
               scope: str | None, c: httpx.Client) -> dict:
    """The client credentials this authorization uses: pre-registered, a cached
    dynamic client, or a new dynamic registration (cached for everyone)."""
    if connector.client_id:
        secret = registry.client_secret(hub_dir, connector.id) if connector.has_client_secret else None
        return {"client_id": connector.client_id, "client_secret": secret,
                "auth_method": _pre_registered_method(disc.token_auth_methods, bool(secret)),
                "source": "pre-registered"}
    cached = registry.registration(hub_dir, connector.id, redirect)
    if (cached and cached.get("issuer") == disc.issuer and cached.get("scope") == scope
            and not registry._expired(cached)):  # noqa: SLF001 — same package
        return cached
    if not disc.registration_endpoint:
        raise ConnectorError("registration_unavailable", "This server does not let Hubzoid "
                             "register itself. An administrator can register Hubzoid with the "
                             "provider and enter its client ID on the connector.", 409)
    return registry.save_registration(hub_dir, connector.id, redirect,
                                      _register(c, disc, redirect, scope))


# ---------------------------------------------------------------------------
# Start
# ---------------------------------------------------------------------------
def start(hub_dir, connector: registry.Connector | None, user, *, origin: str,
          return_to: str | None = None, journey_id: str | None = None) -> str:
    """Begin an authorization for ``user`` (an ``AuthUser``). Returns the
    provider URL the browser goes to. Raises ConnectorError."""
    _require_account(user)
    if connector is None:
        raise ConnectorError("not_found", "No connector has this ID.", 404)
    if not connector.enabled:
        raise ConnectorError("disabled", f"{connector.name} is switched off. Ask your "
                             "administrator.", 409)
    if connector.auth_type != "oauth":
        raise ConnectorError("no_authorization", f"{connector.name} needs no sign-in.", 409)
    if return_to is not None and safe_return_to(return_to) is None:
        raise ConnectorError("invalid_return_to", "return_to must be a path on this site.", 422)
    redirect = redirect_uri(origin, connector.id)
    with net.client(connector.url) as c:
        disc = _discover(connector.url, c)
        scope = connector.scopes or disc.default_scope()
        client = client_for(hub_dir, connector, disc, redirect, scope, c)

    from mcp.client.auth.oauth2 import PKCEParameters

    pkce = PKCEParameters.generate()
    state = secrets.token_urlsafe(32)
    params = {"response_type": "code", "client_id": client["client_id"],
              "redirect_uri": redirect, "state": state,
              "code_challenge": pkce.code_challenge, "code_challenge_method": "S256"}
    if disc.resource:
        params["resource"] = disc.resource
    if scope:
        params["scope"] = scope
    authorize_url = with_params(disc.authorization_endpoint, params)
    payload = {
        "v": 1, "code_verifier": pkce.code_verifier, "redirect_uri": redirect,
        "issuer": disc.issuer, "iss_required": disc.iss_supported,
        "token_endpoint": disc.token_endpoint, "revocation_endpoint": disc.revocation_endpoint,
        "resource": disc.resource, "scope": scope,
        "client": {k: client.get(k) for k in ("client_id", "client_secret", "auth_method", "source")},
        "connector_url": connector.url, "email": normalize(user.email), "journey_id": journey_id,
    }
    now = time.time()
    with engine(hub_dir).begin() as conn:
        conn.execute(text("DELETE FROM hz_connector_flows WHERE expires_at < :now"), {"now": now})
        old = conn.execute(text(
            "SELECT state FROM hz_connector_flows WHERE user_id = :u ORDER BY created_at DESC"),
            {"u": user.id}).fetchall()
        for (digest,) in old[MAX_OPEN_FLOWS - 1:]:
            conn.execute(text("DELETE FROM hz_connector_flows WHERE state = :s"), {"s": digest})
        conn.execute(text(
            "INSERT INTO hz_connector_flows (state, user_id, connector_id, payload_enc, return_to, "
            "created_at, expires_at) VALUES (:s, :u, :c, :p, :r, :now, :exp)"),
            {"s": _digest(state), "u": user.id, "c": connector.id,
             "p": secretbox.encrypt_json(Path(hub_dir), payload), "r": return_to, "now": now,
             "exp": now + FLOW_SECONDS})
    log.info("connectors: %s started connecting %s", normalize(user.email), connector.id)
    return authorize_url


def _discover(url: str, c: httpx.Client) -> Discovery:
    """Discovery for a connect, reused for a minute: one person retrying, or
    several people connecting at once, do not each fetch every document."""
    now = time.monotonic()
    with _discovered_lock:
        hit = _discovered.get(url)
        if hit and now - hit[0] < DISCOVERY_SECONDS:
            return copy.copy(hit[1])
    found = discover(url, c=c)
    with _discovered_lock:
        _discovered[url] = (now, copy.copy(found))
    return found


def forget_discovery(url: str | None = None) -> None:
    """Drop cached discovery (all, or one server's), for tests and URL changes."""
    with _discovered_lock:
        if url is None:
            _discovered.clear()
        else:
            _discovered.pop(url, None)


def _require_account(user) -> None:
    """Every flow and connection is keyed by the account id: never by nothing."""
    if not getattr(user, "id", None) or not getattr(user, "email", None):
        raise ConnectorError("unauthenticated", "Sign in to continue.", 401)


def connect_without_auth(hub_dir, connector: registry.Connector, user) -> None:
    """Turn on a connector that needs no sign-in for ``user``."""
    _require_account(user)
    if not connector.enabled:
        raise ConnectorError("disabled", f"{connector.name} is switched off. Ask your "
                             "administrator.", 409)
    if connector.auth_type != "none":
        raise ConnectorError("needs_authorization", f"{connector.name} needs you to sign in.", 409)
    tokens.store(hub_dir, user_id=user.id, email=user.email, connector_id=connector.id,
                 token={"v": 1, "kind": "none", "url": connector.url})


# ---------------------------------------------------------------------------
# Callback
# ---------------------------------------------------------------------------
_ERRORS = {
    "invalid_state": "This sign-in link is not valid or was already used. Start connecting again.",
    "expired": "The sign-in took too long. Start connecting again.",
    "wrong_user": "This sign-in was started by a different account.",
    "issuer_mismatch": "The sign-in came back from an unexpected authorization server, so it "
                       "was refused.",
    "access_denied": "Access was not granted.",
    "authorization_failed": "The provider could not complete the sign-in.",
    "invalid_request": "The provider's answer was incomplete. Start connecting again.",
    "connector_changed": "This connector was changed or switched off while you were signing in.",
    "token_exchange_failed": "The provider did not issue access. Start connecting again.",
}


def _fail(code: str, **ctx) -> CallbackError:
    return CallbackError(code, _ERRORS[code], **ctx)


def _consume(hub_dir, state: str):
    """Take the flow for ``state`` exactly once: (row) or None."""
    digest = _digest(state)
    with engine(hub_dir).begin() as conn:
        r = conn.execute(text(
            "SELECT user_id, connector_id, payload_enc, return_to, expires_at "
            "FROM hz_connector_flows WHERE state = :s"), {"s": digest}).fetchone()
        if not r:
            return None
        res = conn.execute(text("DELETE FROM hz_connector_flows WHERE state = :s"), {"s": digest})
        if res.rowcount != 1:
            return None
    return r


def _exchange(hub_dir, connector_id: str, payload: dict, code: str, ctx: dict):
    from mcp.shared.auth import OAuthToken

    data = {"grant_type": "authorization_code", "code": code,
            "redirect_uri": payload["redirect_uri"], "code_verifier": payload["code_verifier"]}
    if payload.get("resource"):
        data["resource"] = payload["resource"]
    client = payload.get("client") or {}
    data, headers = net.client_auth(client, data)
    try:
        with net.client(payload.get("connector_url")) as c:
            status, body = net.post_json(c, payload["token_endpoint"], data=data, headers=headers)
    except httpx.HTTPError as exc:
        reason = "address rule" if net.refused_host(exc) else type(exc).__name__
        log.warning("connectors: token endpoint for %s unreachable (%s)", connector_id, reason)
        raise _fail("token_exchange_failed", **ctx) from None
    if status != 200 or not body:
        err = net.oauth_error(body)
        log.warning("connectors: code exchange for %s refused (%s, %s)", connector_id, status, err)
        if err == "invalid_client" and client.get("source") == "dynamic":
            registry.forget_registration(hub_dir, connector_id, payload["redirect_uri"])
        raise _fail("token_exchange_failed", **ctx)
    try:
        return OAuthToken.model_validate(body)
    except ValidationError:
        log.warning("connectors: token response for %s was not a bearer token", connector_id)
        raise _fail("token_exchange_failed", **ctx) from None


def callback(hub_dir, *, connector_id: str, user, state: str | None, code: str | None,
             iss: str | None = None, error: str | None = None) -> Completed:
    """Finish an authorization for the signed-in ``user``. Raises CallbackError
    (never a secret in it); the flow is consumed either way."""
    _require_account(user)
    if not state or len(state) > 256:
        raise _fail("invalid_state", connector_id=connector_id)
    row = _consume(hub_dir, state)
    if row is None:
        raise _fail("invalid_state", connector_id=connector_id)
    flow_user, flow_connector, payload_enc, return_to, expires_at = row
    try:
        payload = secretbox.decrypt_json(Path(hub_dir), payload_enc)
    except (secretbox.SecretKeyError, ValueError):
        raise _fail("invalid_state", connector_id=connector_id) from None
    ctx = {"connector_id": flow_connector, "return_to": return_to,
           "journey_id": payload.get("journey_id"), "email": payload.get("email")}
    if flow_connector != connector_id:
        raise _fail("invalid_state", connector_id=connector_id)
    if flow_user != user.id:
        # Never send another person to the owner's page.
        raise _fail("wrong_user", connector_id=connector_id, journey_id=ctx["journey_id"],
                    email=ctx["email"])
    if time.time() > float(expires_at):
        raise _fail("expired", **ctx)
    expected = payload.get("issuer")
    if (iss is not None and iss != expected) or (iss is None and payload.get("iss_required")):
        log.warning("connectors: %s callback for %s carried an unexpected issuer", connector_id,
                    normalize(user.email))
        raise _fail("issuer_mismatch", **ctx)
    if error:
        raise _fail("access_denied" if error == "access_denied" else "authorization_failed", **ctx)
    if not code or len(code) > 4096:
        raise _fail("invalid_request", **ctx)
    connector = registry.get(hub_dir, connector_id)
    if (connector is None or not connector.enabled or connector.auth_type != "oauth"
            or connector.url != payload.get("connector_url")):
        raise _fail("connector_changed", **ctx)
    token = _exchange(hub_dir, connector_id, payload, code, ctx)
    now = time.time()
    client = payload.get("client") or {}
    record = {
        "v": 1, "kind": "oauth",
        "access_token": token.access_token, "refresh_token": token.refresh_token,
        "token_type": token.token_type, "scope": token.scope or payload.get("scope"),
        "expires_at": now + int(token.expires_in) if token.expires_in is not None else None,
        "token_endpoint": payload["token_endpoint"],
        "revocation_endpoint": payload.get("revocation_endpoint"),
        "resource": payload.get("resource"), "issuer": expected,
        "client": client, "redirect_uri": payload["redirect_uri"],
        "url": connector.url,
    }
    # The exchange took a moment: if the connector was removed, switched off or
    # pointed elsewhere meanwhile, these tokens must not be kept or left live.
    now_connector = registry.get(hub_dir, connector_id)
    if (now_connector is None or not now_connector.enabled or now_connector.auth_type != "oauth"
            or now_connector.url != connector.url):
        tokens.revoke_later(record)
        raise _fail("connector_changed", **ctx)
    # Some providers issue a refresh token on the first consent only: a new
    # authorization by the same client at the same issuer keeps the stored one.
    tokens.store(hub_dir, user_id=user.id, email=user.email, connector_id=connector_id,
                 token=record, now=now, inherit_refresh=(expected, client.get("client_id")))
    log.info("connectors: %s connected %s", normalize(user.email), connector_id)
    return Completed(connector_id=connector_id, return_to=return_to,
                     journey_id=payload.get("journey_id"))
