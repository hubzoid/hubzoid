"""Check a person's token for a registered app and return the caller.

The token is a JWT. Its issuer and audience pick exactly one app
(``config.AppsService.by_issuer``); that app's key checks it:

* ``secret_ref`` (a pass, or an app JWT signed with a shared secret): HS256 only;
* ``public_key``: the algorithms of that key's type (RS*/PS* or ES*);
* ``jwks_url``: the app's published keys, fetched over https, no redirects,
  cached; an unknown ``kid`` fetches once more, and stale keys are used at most
  an hour while the URL fails.

Required: ``iss``, ``aud``, ``iat``, ``exp`` and the user claim (a non-empty
string). ``exp - iat`` is at most the app's ``max_lifetime``; 60 s of clock
skew is allowed. ``none`` and algorithms of another key type are refused, so a
public key can never be used as an HMAC secret.

How others do it: Agno checks with keys you configure
(``AuthorizationConfig(verification_keys, jwks_file)``, ``agno/os/config.py``),
Mastra with an auth provider (``MastraJwtAuth``, ``JWT_AUTH_SECRET``), eve with
``jwtHmac``/``jwtEcdsa``/``oidc``. None of them create user tokens; neither do we.
"""
from __future__ import annotations

import base64
import json
import math
import logging
import threading
import time
from pathlib import Path
from typing import Any

from .caller import AppCaller, effective, subject
from .config import Access, App, AppsError, service_for

log = logging.getLogger("hubzoid.embed")

SKEW = 60
MAX_TOKEN_BYTES = 8 * 1024
MAX_APP_TOKEN_BYTES = 8 * 1024
MIN_SECRET_BYTES = 32
_JWKS_FRESH = 600
_JWKS_STALE = 3600
_JWKS_REFRESH_GAP = 60
_JWKS_MAX_BYTES = 256 * 1024
RSA_ALGS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512")
EC_ALGS = ("ES256", "ES384", "ES512")
OKP_ALGS = ("EdDSA",)

_jwks: dict[str, tuple[float, float, Any]] = {}   # url -> (fetched_at, last_try, KeySet)
_jwks_lock = threading.Lock()


def refused(code: str, message: str, status: int = 401) -> AppsError:
    return AppsError(status, code, message)


def _segment(seg: str) -> dict:
    try:
        raw = base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))
        value = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        raise refused("invalid_token", "The token can't be read.") from exc
    if not isinstance(value, dict):
        raise refused("invalid_token", "The token can't be read.")
    return value


def peek(token: str) -> tuple[dict, dict]:
    """(header, claims) without checking the signature: only to pick the app."""
    if not isinstance(token, str) or len(token.encode("utf-8", "ignore")) > MAX_TOKEN_BYTES:
        raise refused("invalid_token", "The token is missing or too large.")
    parts = token.split(".")
    if len(parts) != 3 or not all(parts[:2]):
        raise refused("invalid_token", "The token is not a JWT.")
    return _segment(parts[0]), _segment(parts[1])


def _secret(hub_dir: Path, app: App) -> bytes:
    from ..secretstore import resolve

    try:
        value = resolve(app.secret_ref, hub_dir)
    except Exception as exc:  # noqa: BLE001 - a missing secret refuses every token
        log.warning("embed: secret for app %s unavailable (%s)", app.id, type(exc).__name__)
        raise refused("app_unavailable", "This app's key is not set up in Hubzoid.", 503) from exc
    raw = value.encode("utf-8")
    if len(raw) < MIN_SECRET_BYTES:
        raise refused("app_unavailable", "This app's secret is too short (32 characters at least).", 503)
    return raw


def _fetch_jwks(url: str):
    import httpx
    from joserfc.jwk import KeySet

    with httpx.Client(timeout=5.0, follow_redirects=False) as client:
        with client.stream("GET", url, headers={"Accept": "application/json"}) as resp:
            if resp.status_code != 200:
                raise ValueError(f"status {resp.status_code}")
            body = b""
            for chunk in resp.iter_bytes():
                body += chunk
                if len(body) > _JWKS_MAX_BYTES:
                    raise ValueError("too large")
    data = json.loads(body)
    keys = []
    for raw in (data.get("keys") or []) if isinstance(data, dict) else []:
        if not isinstance(raw, dict) or raw.get("use", "sig") != "sig" or raw.get("kty") == "oct":
            continue  # never a symmetric key from a URL
        try:
            keys.append(KeySet.import_key_set({"keys": [raw]}).keys[0])
        except Exception:  # noqa: BLE001 - skip a key type we can't use
            continue
    if not keys:
        raise ValueError("no usable signing keys")
    return KeySet(keys)


def _keyset(url: str, *, refresh: bool = False):
    now = time.time()
    with _jwks_lock:
        hit = _jwks.get(url)
    if hit and not refresh and now - hit[0] < _JWKS_FRESH:
        return hit[2]
    if hit and refresh and now - hit[1] < _JWKS_REFRESH_GAP:
        return hit[2]
    try:
        keyset = _fetch_jwks(url)
    except Exception as exc:  # noqa: BLE001
        log.warning("embed: keys at %s unavailable (%s)", url, type(exc).__name__)
        if hit and now - hit[0] < _JWKS_STALE:
            with _jwks_lock:
                _jwks[url] = (hit[0], now, hit[2])
            return hit[2]
        raise refused("app_unavailable", "The app's signing keys can't be fetched right now.", 503) from exc
    with _jwks_lock:
        _jwks[url] = (now, now, keyset)
    return keyset


def _algorithms_for(key) -> tuple[str, ...]:
    kind = getattr(key, "key_type", "")
    return {"RSA": RSA_ALGS, "EC": EC_ALGS, "OKP": OKP_ALGS}.get(kind, ())


def _key_and_algs(hub_dir: Path, app: App, header: dict):
    from joserfc.errors import InvalidKeyIdError
    from joserfc.jwk import ECKey, OctKey, RSAKey

    if app.secret_ref:
        return OctKey.import_key(_secret(hub_dir, app)), ("HS256",)
    if app.public_key:
        pem = app.public_key.encode("utf-8")
        for cls in (RSAKey, ECKey):
            try:
                key = cls.import_key(pem)
                return key, _algorithms_for(key)
            except Exception:  # noqa: BLE001 - try the next type
                continue
        raise refused("app_unavailable", "This app's public key can't be read.", 503)
    keyset = _keyset(app.jwks_url)
    kid = header.get("kid")
    try:
        key = keyset.get_by_kid(kid) if kid else (keyset.keys[0] if len(keyset.keys) == 1 else None)
    except (InvalidKeyIdError, ValueError):
        key = None
    if key is None and kid:
        keyset = _keyset(app.jwks_url, refresh=True)
        try:
            key = keyset.get_by_kid(kid)
        except (InvalidKeyIdError, ValueError):
            key = None
    if key is None:
        raise refused("invalid_token", "The token's signing key is not one of the app's keys.")
    return key, _algorithms_for(key)


def _number(claims: dict, name: str) -> float:
    value = claims.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise refused("invalid_token", f"The token has no valid {name} claim.")
    return float(value)


def _requested(raw) -> Access | None:
    """A pass's ``access``: ``{agent | agents, tools}``. None: the maximum."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise refused("invalid_token", "access is {agent or agents, tools}.")
    agents = raw.get("agents", raw.get("agent"))
    tools = raw.get("tools")
    if isinstance(agents, str):
        agents = [agents]
    if agents is not None and (not isinstance(agents, list) or not all(isinstance(a, str) for a in agents)):
        raise refused("invalid_token", "access.agents is a list of agent names.")
    if tools is not None and (not isinstance(tools, list) or not all(isinstance(t, str) for t in tools)):
        raise refused("invalid_token", "access.tools is a list of tool names.")
    return Access(agents=frozenset((a or "").strip().lower() for a in (agents or []) if a.strip()),
                  tools=frozenset(t.strip() for t in tools if t.strip()) if tools is not None else frozenset({"*"}))


def _scoped(app: App, claims: dict) -> Access | None:
    """jwt mode: the access the token's scope values map to (union), or the
    maximum when the app opted in to all users. None: nothing granted."""
    if app.all_users:
        return app.maximum
    raw = claims.get(app.scope_claim or "scope")
    values = raw.split() if isinstance(raw, str) else (raw if isinstance(raw, list) else [])
    agents: set[str] = set()
    tools: set[str] = set()
    for value in values:
        grant = app.scope_map.get(str(value))
        if grant is not None:
            agents |= grant.agents
            tools |= grant.tools
    if not agents:
        return None
    return Access(agents=frozenset(agents), tools=frozenset(tools))


def verify(hub_dir: Path, token: str, *, now: float | None = None) -> AppCaller:
    """The caller ``token`` names, or AppsError (401 invalid, 403 no access,
    503 when the app's keys are unavailable)."""
    from joserfc import jwt
    from joserfc.errors import JoseError

    now = time.time() if now is None else now
    header, unverified = peek(token)
    app = service_for(hub_dir).by_issuer(str(unverified.get("iss") or ""), unverified.get("aud"))
    if app is None:
        raise refused("unknown_app", "No app registered in Hubzoid issued this token.")
    if not app.enabled:
        raise refused("app_disabled", "This app is turned off in Hubzoid.", 403)
    alg = header.get("alg")
    if not isinstance(alg, str) or alg.lower() == "none":
        raise refused("invalid_token", "The token is not signed.")
    key, algorithms = _key_and_algs(hub_dir, app, header)
    if alg not in algorithms:
        raise refused("invalid_token", "The token is signed with an algorithm this app doesn't use.")
    try:
        claims = dict(jwt.decode(token, key, algorithms=list(algorithms)).claims)
    except (JoseError, ValueError) as exc:
        raise refused("invalid_token", "The token's signature doesn't match.") from exc
    # The signed claims decide, never the peeked ones.
    if claims.get("iss") != app.issuer:
        raise refused("invalid_token", "The token's issuer doesn't match.")
    aud = claims.get("aud")
    if not (aud == app.audience or (isinstance(aud, list) and app.audience in aud)):
        raise refused("invalid_token", "The token is not meant for Hubzoid.")
    iat, exp = _number(claims, "iat"), _number(claims, "exp")
    if "nbf" in claims and _number(claims, "nbf") > now + SKEW:
        raise refused("invalid_token", "The token is not valid yet.")
    if iat > now + SKEW:
        raise refused("invalid_token", "The token was issued in the future.")
    if exp <= now - SKEW:
        raise refused("token_expired", "The token has expired. Get a new one from the app.")
    if exp - iat > app.max_lifetime or exp <= iat:
        raise refused("invalid_token", f"The token may last at most {app.max_lifetime} seconds.")
    user_id = claims.get(app.user_claim)
    # The id exactly as signed: "alice" and " alice " are different people.
    if not isinstance(user_id, str) or not user_id.strip() or len(user_id) > 256:
        raise refused("invalid_token", f"The token has no {app.user_claim} claim naming the person.")
    if app.mode == "pass":
        access = effective(app, _requested(claims.get("access")))
        app_token = claims.get("app_token")
        if app_token is not None and (not isinstance(app_token, str)
                                      or len(app_token.encode("utf-8", "ignore")) > MAX_APP_TOKEN_BYTES):
            raise refused("invalid_token", "app_token is the person's app token, as text.")
    else:
        granted = _scoped(app, claims)
        access = effective(app, granted) if granted is not None else None
        app_token = token
    if access is None or not access.agents:
        raise refused("no_access", "This person has no access to any agent through this app.", 403)

    def label(name: str) -> str:
        value = claims.get(name)
        return value.strip()[:320] if isinstance(value, str) else ""

    return AppCaller(app_id=app.id, user_id=user_id, subject=subject(app.id, user_id),
                     name=label(app.name_claim), email=label(app.email_claim), access=access,
                     issued_at=iat, expires_at=exp, app_token=app_token or None)


def reset_cache() -> None:
    with _jwks_lock:
        _jwks.clear()
