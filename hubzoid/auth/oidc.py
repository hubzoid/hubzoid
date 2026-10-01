"""External sign-in with OpenID Connect: Google, Microsoft and one standard
provider (Okta, Auth0, Keycloak, authentik, ...).

Configuration uses Open WebUI's names, so an existing deployment keeps working:

  * Google: ``GOOGLE_CLIENT_ID``, ``GOOGLE_CLIENT_SECRET`` (``GOOGLE_OAUTH_SCOPE``)
  * Microsoft: ``MICROSOFT_CLIENT_ID``, ``MICROSOFT_CLIENT_SECRET``,
    ``MICROSOFT_CLIENT_TENANT_ID`` (default ``common``; ``MICROSOFT_OAUTH_SCOPE``)
  * standard OIDC: ``OPENID_PROVIDER_URL`` (the discovery document, or the
    issuer it belongs to), ``OAUTH_CLIENT_ID``, ``OAUTH_CLIENT_SECRET``,
    ``OAUTH_PROVIDER_NAME``, ``OAUTH_SCOPES``
  * policy: ``OAUTH_MERGE_ACCOUNTS_BY_EMAIL``, ``ENABLE_OAUTH_SIGNUP``,
    ``OAUTH_ALLOWED_DOMAINS``

The flow is the authorization code flow with PKCE (S256), ``state`` and
``nonce``. No server-side session store is involved: the short handshake
(provider, state, nonce, verifier, return path, expiry of 10 minutes) travels
in an encrypted, authenticated, HttpOnly cookie scoped to ``/oauth``. The ID
token's signature is checked against the provider's published keys (JWKS),
then its issuer, audience, expiry and nonce.

Who signs in (contract 6.1):
  * An external sign-in is keyed on (issuer, subject) and linked to one
    account in ``hz_user_identities``.
  * A first sign-in attaches to an existing account with the same email only
    when the provider marks the email verified and
    ``OAUTH_MERGE_ACCOUNTS_BY_EMAIL`` is true. Never on an unverified email.
  * ``OAUTH_ALLOWED_DOMAINS`` (``*`` or unset: any) limits every external
    sign-in to those email domains, judged on a verified email only (a linked
    sign-in whose current email is unverified is judged on its account's
    email). For Google a work domain must also be the account's Google
    Workspace domain (``hd``), so a personal Google account registered with a
    work address is refused.
  * With ``ENABLE_OAUTH_SIGNUP`` a new account is created ``pending`` (an
    administrator approves it), or ``active`` when its verified email is in an
    explicitly allowed domain. Without it: ``/auth?error=no_account``.
"""
from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlencode, urlsplit

import httpx

from .. import appmode

log = logging.getLogger("hubzoid.auth")

GOOGLE_DISCOVERY = "https://accounts.google.com/.well-known/openid-configuration"
MICROSOFT_DISCOVERY = ("https://login.microsoftonline.com/{tenant}/v2.0/"
                       ".well-known/openid-configuration")
WELL_KNOWN = "/.well-known/openid-configuration"
DEFAULT_SCOPES = "openid email profile"
HANDSHAKE_COOKIE = "hz_oauth"
HANDSHAKE_PATH = "/oauth"
HANDSHAKE_SECONDS = 600
PROVIDER_IDS = ("google", "microsoft", "oidc")
# Asymmetric signatures only: never "none" or a shared-secret HMAC.
SAFE_ALGORITHMS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512",
                   "ES256", "ES384", "ES512", "EdDSA")
# Consumer Google addresses have no Workspace domain (hd).
GOOGLE_CONSUMER_DOMAINS = frozenset({"gmail.com", "googlemail.com"})
_TID = re.compile(r"^[0-9a-fA-F-]{36}$")
_TIMEOUT = httpx.Timeout(10.0)
_CACHE_SECONDS = 3600
_JWKS_REFRESH_SECONDS = 60
LEEWAY_SECONDS = 120


class SignInRefused(Exception):
    """A sign-in that must not go ahead. ``code`` goes to ``/auth?error=``."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code


@dataclass(frozen=True)
class Provider:
    id: str
    name: str
    client_id: str
    client_secret: str
    discovery_url: str
    scopes: str
    extra_issuers: tuple[str, ...] = ()


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def _get(env: Mapping[str, str], key: str) -> str:
    return (env.get(key) or "").strip()


def _true(env: Mapping[str, str], key: str) -> bool:
    return _get(env, key).lower() in ("1", "true", "yes", "on")


def _scopes(raw: str) -> str:
    parts = [s for s in re.split(r"[\s,]+", raw or "") if s] or DEFAULT_SCOPES.split()
    if "openid" not in parts:
        parts.insert(0, "openid")
    return " ".join(dict.fromkeys(parts))


def _discovery_for(url: str) -> str:
    url = url.strip()
    return url if url.rstrip("/").endswith(WELL_KNOWN) else url.rstrip("/") + WELL_KNOWN


def providers(env: Mapping[str, str] | None = None) -> list[Provider]:
    """The configured providers, in sign-in page order."""
    env = _env(env)
    out: list[Provider] = []
    if _get(env, "GOOGLE_CLIENT_ID") and _get(env, "GOOGLE_CLIENT_SECRET"):
        out.append(Provider(
            "google", "Google", _get(env, "GOOGLE_CLIENT_ID"), _get(env, "GOOGLE_CLIENT_SECRET"),
            GOOGLE_DISCOVERY, _scopes(_get(env, "GOOGLE_OAUTH_SCOPE")),
            extra_issuers=("accounts.google.com",)))
    if _get(env, "MICROSOFT_CLIENT_ID") and _get(env, "MICROSOFT_CLIENT_SECRET"):
        tenant = _get(env, "MICROSOFT_CLIENT_TENANT_ID") or "common"
        if not re.match(r"^[A-Za-z0-9.-]{1,100}$", tenant):
            log.error("auth: MICROSOFT_CLIENT_TENANT_ID is not a tenant id or name; "
                      "Microsoft sign-in is off")
        else:
            out.append(Provider(
                "microsoft", "Microsoft", _get(env, "MICROSOFT_CLIENT_ID"),
                _get(env, "MICROSOFT_CLIENT_SECRET"), MICROSOFT_DISCOVERY.format(tenant=tenant),
                _scopes(_get(env, "MICROSOFT_OAUTH_SCOPE"))))
    if _get(env, "OPENID_PROVIDER_URL") and _get(env, "OAUTH_CLIENT_ID"):
        out.append(Provider(
            "oidc", _get(env, "OAUTH_PROVIDER_NAME") or "SSO", _get(env, "OAUTH_CLIENT_ID"),
            _get(env, "OAUTH_CLIENT_SECRET"), _discovery_for(_get(env, "OPENID_PROVIDER_URL")),
            _scopes(_get(env, "OAUTH_SCOPES"))))
    return out


def provider(provider_id: str, env: Mapping[str, str] | None = None) -> Provider | None:
    return next((p for p in providers(env) if p.id == provider_id), None)


def public_list(env: Mapping[str, str] | None = None) -> list[dict]:
    return [{"id": p.id, "name": p.name} for p in providers(env)]


# ---- policy ---------------------------------------------------------------------

def allowed_domains(env: Mapping[str, str] | None = None) -> list[str] | None:
    """The domains external sign-in accepts, or None for any (unset or ``*``),
    parsed as Open WebUI parses ``OAUTH_ALLOWED_DOMAINS``."""
    raw = _env(env).get("OAUTH_ALLOWED_DOMAINS")
    if raw is None:
        return None
    domains = [d.strip().lower() for d in raw.split(",")]
    if "*" in domains:
        return None
    return [d for d in domains if d]


def merge_by_email(env: Mapping[str, str] | None = None) -> bool:
    return _true(_env(env), "OAUTH_MERGE_ACCOUNTS_BY_EMAIL")


def signup_enabled(env: Mapping[str, str] | None = None) -> bool:
    return _true(_env(env), "ENABLE_OAUTH_SIGNUP")


def email_verified(provider_id: str, claims: Mapping) -> bool:
    """Whether the provider vouches for the email. ``email_verified`` true, or
    for Microsoft the ``xms_edov`` optional claim (domain owner verified)."""
    def yes(value) -> bool:
        return value is True or (isinstance(value, str) and value.strip().lower() == "true")

    if yes(claims.get("email_verified")):
        return True
    return provider_id == "microsoft" and (yes(claims.get("xms_edov")) or claims.get("xms_edov") == 1)


def domain_allowed(provider_id: str, email: str, claims: Mapping,
                   domains: list[str] | None) -> bool:
    if domains is None:
        return True
    domain = email.rsplit("@", 1)[-1].lower() if "@" in email else ""
    if not domain or domain not in domains:
        return False
    if provider_id == "google":
        hd = str(claims.get("hd") or "").strip().lower()
        if hd:
            return hd in domains
        return domain in GOOGLE_CONSUMER_DOMAINS
    return True


def _secure_url(url: str) -> bool:
    try:
        p = urlsplit(url)
    except ValueError:
        return False
    if p.scheme == "https" and p.netloc:
        return True
    return p.scheme == "http" and p.hostname in ("localhost", "127.0.0.1", "::1")


# ---- discovery and keys ---------------------------------------------------------

_cache: dict[str, tuple[float, object]] = {}
_jwks_fetched: dict[str, float] = {}
_cache_lock = threading.Lock()


def reset_cache() -> None:
    with _cache_lock:
        _cache.clear()
        _jwks_fetched.clear()


def _fetch_json(url: str) -> dict:
    if not _secure_url(url):
        raise SignInRefused("provider_error", f"refusing a non-https provider URL: {url}")
    r = httpx.get(url, timeout=_TIMEOUT, follow_redirects=False,
                  headers={"Accept": "application/json"})
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    return data


def discovery(p: Provider) -> dict:
    """The provider's metadata (cached for an hour)."""
    now = time.time()
    with _cache_lock:
        hit = _cache.get("d:" + p.discovery_url)
    if hit and hit[0] > now:
        return hit[1]  # type: ignore[return-value]
    try:
        meta = _fetch_json(p.discovery_url)
    except SignInRefused:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("auth: %s discovery failed (%s)", p.id, type(exc).__name__)
        raise SignInRefused("provider_error", "discovery failed") from exc
    for key in ("issuer", "authorization_endpoint", "token_endpoint", "jwks_uri"):
        if not isinstance(meta.get(key), str) or not meta[key]:
            raise SignInRefused("provider_error", f"discovery document lacks {key}")
    for key in ("authorization_endpoint", "token_endpoint", "jwks_uri", "userinfo_endpoint"):
        if meta.get(key) and not _secure_url(meta[key]):
            raise SignInRefused("provider_error", f"{key} is not https")
    with _cache_lock:
        _cache["d:" + p.discovery_url] = (now + _CACHE_SECONDS, meta)
    return meta


def _keyset(jwks_uri: str, *, refresh: bool = False):
    from joserfc.jwk import KeySet

    now = time.time()
    with _cache_lock:
        hit = _cache.get("k:" + jwks_uri)
        last = _jwks_fetched.get(jwks_uri, 0.0)
    if hit and hit[0] > now and not (refresh and now - last >= _JWKS_REFRESH_SECONDS):
        return hit[1]
    try:
        data = _fetch_json(jwks_uri)
        keys = []
        for raw in data.get("keys") or []:
            if not isinstance(raw, dict) or raw.get("use", "sig") != "sig":
                continue
            try:
                keys.append(KeySet.import_key_set({"keys": [raw]}).keys[0])
            except Exception:  # noqa: BLE001 — skip a key type we can't use
                continue
        if not keys:
            raise ValueError("no usable signing keys")
        keyset = KeySet(keys)
    except Exception as exc:  # noqa: BLE001
        if hit:
            return hit[1]
        log.warning("auth: provider keys unavailable (%s)", type(exc).__name__)
        raise SignInRefused("provider_error", "keys unavailable") from exc
    with _cache_lock:
        _cache["k:" + jwks_uri] = (now + _CACHE_SECONDS, keyset)
        _jwks_fetched[jwks_uri] = now
    return keyset


def _header(token: str) -> dict:
    try:
        segment = token.split(".")[0]
        padded = segment + "=" * (-len(segment) % 4)
        header = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except Exception as exc:  # noqa: BLE001
        raise SignInRefused("invalid_token", "unreadable token header") from exc
    if not isinstance(header, dict):
        raise SignInRefused("invalid_token", "unreadable token header")
    return header


def validate_id_token(p: Provider, meta: Mapping, id_token: str, *, nonce: str,
                      now: float | None = None) -> dict:
    """The ID token's claims, after checking its signature against the
    provider's keys and its issuer, audience, times and nonce."""
    from joserfc import jwt
    from joserfc.errors import InvalidKeyIdError, JoseError

    if not isinstance(id_token, str) or id_token.count(".") != 2:
        raise SignInRefused("invalid_token", "no ID token")
    header = _header(id_token)
    offered = meta.get("id_token_signing_alg_values_supported") or ["RS256"]
    algorithms = [a for a in SAFE_ALGORITHMS if a in offered] or ["RS256"]
    if header.get("alg") not in algorithms:
        raise SignInRefused("invalid_token", "unexpected signing algorithm")
    kid = header.get("kid")
    keyset = _keyset(str(meta["jwks_uri"]))
    try:
        key = keyset.get_by_kid(kid)
    except (InvalidKeyIdError, ValueError):
        keyset = _keyset(str(meta["jwks_uri"]), refresh=True)
        try:
            key = keyset.get_by_kid(kid)
        except (InvalidKeyIdError, ValueError) as exc:
            raise SignInRefused("invalid_token", "unknown signing key") from exc
    try:
        claims = dict(jwt.decode(id_token, key, algorithms=algorithms).claims)
    except (JoseError, ValueError) as exc:
        raise SignInRefused("invalid_token", "bad signature") from exc

    issuer = str(meta["issuer"])
    if "{tenantid}" in issuer:
        tid = str(claims.get("tid") or "")
        if not _TID.match(tid):
            raise SignInRefused("invalid_token", "no tenant")
        issuer = issuer.replace("{tenantid}", tid)
    iss = claims.get("iss")
    if not isinstance(iss, str) or (iss != issuer and iss not in p.extra_issuers):
        raise SignInRefused("invalid_token", "wrong issuer")
    aud = claims.get("aud")
    audiences = [aud] if isinstance(aud, str) else aud if isinstance(aud, list) else []
    if p.client_id not in audiences:
        raise SignInRefused("invalid_token", "wrong audience")
    if len(audiences) > 1 and claims.get("azp") != p.client_id:
        raise SignInRefused("invalid_token", "wrong authorized party")
    now = time.time() if now is None else now
    registry = jwt.JWTClaimsRegistry(
        now=int(now), leeway=LEEWAY_SECONDS,
        exp={"essential": True}, iat={"essential": True}, sub={"essential": True},
    )
    try:
        registry.validate(claims)
    except JoseError as exc:
        raise SignInRefused("invalid_token", "expired or not yet valid") from exc
    if not isinstance(claims.get("sub"), str) or not claims["sub"] or len(claims["sub"]) > 255:
        raise SignInRefused("invalid_token", "bad subject")
    if not hmac.compare_digest(str(claims.get("nonce") or ""), nonce or "\x00"):
        raise SignInRefused("invalid_token", "nonce mismatch")
    # Identities are keyed on the canonical issuer (Google also writes it
    # without the scheme), so one person never becomes two.
    claims["iss"] = issuer
    return claims


# ---- the handshake --------------------------------------------------------------

def safe_redirect(value: str | None, default: str = "/") -> str:
    """``value`` when it is a same-origin relative path (``/x``), else
    ``default``. Never ``//host``, a scheme, a backslash or a control character."""
    v = (value or "").strip()
    if (not v or not v.startswith("/") or v.startswith("//") or "\\" in v or len(v) > 2048
            or any(ord(c) < 32 or ord(c) == 127 for c in v)):
        return default
    try:
        parts = urlsplit(v)
    except ValueError:
        return default
    if parts.scheme or parts.netloc:
        return default
    return v


def callback_url(request, provider_id: str) -> str:
    """Where the provider sends the browser back: the request's own origin
    when it is one of the deployment's origins (two public names both work),
    else the configured public URL, else (local development) the request."""
    from . import sessions as sessionlib

    scheme = "https" if sessionlib.is_https(request) else "http"
    host = request.headers.get("host") or request.url.netloc
    origin = appmode.normalize_origin(f"{scheme}://{host}")
    allowed = appmode.allowed_origins()
    if origin and origin in allowed:
        base = origin
    elif appmode.public_url():
        base = appmode.public_url()
    else:
        base = origin or str(request.base_url).rstrip("/")
    return f"{base}/oauth/{provider_id}/callback"


def start(hub_dir: Path, p: Provider, *, redirect_uri: str, return_to: str) -> tuple[str, str]:
    """(authorization URL, handshake cookie value)."""
    from authlib.oauth2.rfc7636 import create_s256_code_challenge

    from .. import secretbox

    meta = discovery(p)
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    cookie = secretbox.encrypt_json(Path(hub_dir), {
        "p": p.id, "s": state, "n": nonce, "v": verifier, "r": safe_redirect(return_to),
        "u": redirect_uri, "e": time.time() + HANDSHAKE_SECONDS,
    })
    endpoint = str(meta["authorization_endpoint"])
    query = urlencode({
        "response_type": "code", "client_id": p.client_id, "redirect_uri": redirect_uri,
        "scope": p.scopes, "state": state, "nonce": nonce,
        "code_challenge": create_s256_code_challenge(verifier), "code_challenge_method": "S256",
    })
    return endpoint + ("&" if "?" in endpoint else "?") + query, cookie


def read_handshake(hub_dir: Path, value: str | None, provider_id: str) -> dict | None:
    """The handshake from the cookie, when it decrypts, is for this provider
    and has not expired."""
    from .. import secretbox

    if not value or len(value) > 4096:
        return None
    try:
        data = secretbox.decrypt_json(Path(hub_dir), value)
    except Exception:  # noqa: BLE001 — tampered, foreign or rotated-away key
        return None
    if not isinstance(data, dict) or data.get("p") != provider_id:
        return None
    if not isinstance(data.get("e"), (int, float)) or data["e"] < time.time():
        return None
    if not all(isinstance(data.get(k), str) and data[k] for k in ("s", "n", "v", "u")):
        return None
    return data


def _token_auth_method(p: Provider, meta: Mapping) -> str:
    if not p.client_secret:
        return "none"
    supported = meta.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
    if "client_secret_basic" in supported:
        return "client_secret_basic"
    if "client_secret_post" in supported:
        return "client_secret_post"
    return "client_secret_basic"


def finish(p: Provider, handshake: Mapping, *, code: str, state: str) -> dict:
    """Exchange the code and return the verified claims (with ``email`` from
    the userinfo endpoint when the ID token has none)."""
    from authlib.integrations.httpx_client import OAuth2Client

    if not state or not hmac.compare_digest(str(state), str(handshake["s"])):
        raise SignInRefused("state_mismatch")
    if not code or len(code) > 4096:
        raise SignInRefused("provider_error", "no code")
    meta = discovery(p)
    try:
        with OAuth2Client(client_id=p.client_id, client_secret=p.client_secret or None,
                          token_endpoint_auth_method=_token_auth_method(p, meta),
                          timeout=_TIMEOUT) as client:
            token = client.fetch_token(
                str(meta["token_endpoint"]), grant_type="authorization_code", code=code,
                redirect_uri=handshake["u"], code_verifier=handshake["v"])
    except Exception as exc:  # noqa: BLE001 — never echo the provider's response
        log.warning("auth: %s code exchange failed (%s)", p.id, type(exc).__name__)
        raise SignInRefused("provider_error", "code exchange failed") from exc
    claims = validate_id_token(p, meta, token.get("id_token") or "", nonce=handshake["n"])
    if not claims.get("email") and meta.get("userinfo_endpoint") and token.get("access_token"):
        claims = _with_userinfo(claims, str(meta["userinfo_endpoint"]), str(token["access_token"]))
    return claims


def _with_userinfo(claims: dict, url: str, access_token: str) -> dict:
    """Fill a missing email from the userinfo endpoint. Its ``sub`` must match
    the ID token's (OIDC Core 5.3.2), else it is ignored."""
    try:
        r = httpx.get(url, timeout=_TIMEOUT, follow_redirects=False,
                      headers={"Authorization": f"Bearer {access_token}",
                               "Accept": "application/json"})
        r.raise_for_status()
        info = r.json()
    except Exception:  # noqa: BLE001
        log.warning("auth: userinfo request failed")
        return claims
    if not isinstance(info, dict) or info.get("sub") != claims.get("sub"):
        return claims
    merged = dict(claims)
    for key in ("email", "email_verified", "name", "given_name", "family_name"):
        if merged.get(key) is None and info.get(key) is not None:
            merged[key] = info[key]
    return merged


# ---- the account ------------------------------------------------------------------

def _display_name(claims: Mapping, email: str) -> str:
    name = claims.get("name")
    if not isinstance(name, str) or not name.strip():
        parts = [claims.get("given_name"), claims.get("family_name")]
        name = " ".join(str(x).strip() for x in parts if isinstance(x, str) and x.strip())
    if not name:
        name = email.split("@", 1)[0]
    return name.strip()[:200]


def account_for(hub_dir: Path, p: Provider, claims: Mapping,
                env: Mapping[str, str] | None = None) -> dict:
    """The account this external sign-in belongs to, linking or creating it
    under the rules in the module docstring. Raises SignInRefused."""
    from . import users

    st = users.store(Path(hub_dir))
    issuer, subject = str(claims["iss"]), str(claims["sub"])
    email = users.normalize_email(claims.get("email") if isinstance(claims.get("email"), str) else "")
    if email and not re.match(r"^[^\s@]+@[^\s@]+\.[^\s@]+$", email):
        email = ""
    verified = bool(email) and email_verified(p.id, claims)
    domains = allowed_domains(env)
    linked = st.find_identity(issuer, subject)
    user = st.get(linked["user_id"]) if linked else None
    if linked and user is None:
        st.unlink_identity(issuer, subject)  # its account was deleted
        linked = None
    if domains is not None:
        if user is not None:
            checked = email if verified else user["email"]
        elif not verified:
            # Only a verified email can vouch for an allowed domain.
            raise SignInRefused("email_not_verified" if email else "no_email")
        else:
            checked = email
        if not domain_allowed(p.id, checked, claims, domains):
            raise SignInRefused("domain_not_allowed")
    if user is not None:
        st.touch_identity(issuer, subject, email or user["email"])
        return user
    if not email:
        raise SignInRefused("no_email")
    if email.endswith("@localhost") or email.endswith(".localhost"):
        raise SignInRefused("no_account")
    existing = st.find_by_email(email)
    if existing is None and signup_enabled(env):
        active = verified and domains is not None and domain_allowed(p.id, email, claims, domains)
        try:
            existing = st.create(email=email, name=_display_name(claims, email), role="user",
                                 status="active" if active else "pending",
                                 password_enabled=False, source="oidc")
        except users.AccountExists:
            existing = st.find_by_email(email)
            if existing is None:
                raise SignInRefused("unavailable")
        else:
            winner = _link(st, p, issuer, subject, existing, email)
            if winner["id"] != existing["id"]:
                st.delete(existing["id"])  # a concurrent first sign-in linked another account
                return winner
            users.sync_identity(Path(hub_dir), existing)
            log.info("auth: %s sign-up created %s account %s", p.id, existing["status"], email)
            return existing
    if existing is None:
        raise SignInRefused("no_account")
    if not verified:
        raise SignInRefused("email_not_verified")
    if not merge_by_email(env):
        raise SignInRefused("not_linked")
    if existing.get("source") == "local":
        raise SignInRefused("no_account")
    winner = _link(st, p, issuer, subject, existing, email)
    if winner["id"] == existing["id"]:
        log.info("auth: %s sign-in linked to the existing account %s", p.id, email)
    return winner


def _link(st, p: Provider, issuer: str, subject: str, user: dict, email: str) -> dict:
    """Link (issuer, subject) to ``user``. If a concurrent first sign-in
    linked it first, the account the identity is linked to wins: the identity
    is the key, never the email this request happened to carry."""
    from . import users

    try:
        st.link_identity(provider=p.id, issuer=issuer, subject=subject, user_id=user["id"],
                         email=email)
        return user
    except users.AccountExists:
        linked = st.find_identity(issuer, subject)
        winner = st.get(linked["user_id"]) if linked else None
        if winner is None:
            raise SignInRefused("unavailable")
        return winner
