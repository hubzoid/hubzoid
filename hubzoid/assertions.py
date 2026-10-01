"""Signed identity assertions between Hubzoid's own processes.

Hubzoid's surfaces reach a hub's bridge (``/v1/chat/completions``) on loopback
with the bridge API key and say who is asking in headers:
``X-OpenWebUI-User-Email`` or ``X-Hubzoid-User`` (the person),
``X-Hubzoid-Groups`` (groups the surface resolved) and ``X-Hubzoid-Surface``.

In the web app mode the API key alone no longer vouches for those headers. The
caller signs them: ``X-Hubzoid-Assertion`` is an HMAC (``secretbox.sign``, the
deployment key shared by every process of the deployment) over exactly those
values, for this hub, issued in the last 60 seconds. The bridge trusts the
headers only when a valid assertion covers them exactly; otherwise the request
is anonymous (an agent whose access is managed in the Console refuses it, any
other answers without restricted tools). A bridge API key therefore can't be
used to act as somebody else, and the edge strips every client-sent
``X-Hubzoid-*`` header anyway.

The legacy Open WebUI mode keeps the 1.0.x rule (Open WebUI and the adapters
hold the bridge key, so their headers are trusted) and its callers send no
assertion.

Token: ``v1.<base64url(payload JSON)>.<hex HMAC-SHA256>``, the HMAC taken over
``hubzoid-identity-assertion.v1.<base64url part>``. Payload:
``{"v": 1, "hub": <hub key>, "email": <normalized or "">,
"groups": [<normalized names, sorted>], "surface": <normalized>, "iat": <unix seconds>}``.
Never log a token.
"""
from __future__ import annotations

import base64
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from .access.identity import normalize

log = logging.getLogger("hubzoid.assertions")

HEADER = "X-Hubzoid-Assertion"
VALIDITY_SECONDS = 60
# Processes of one deployment share a clock, but a bridge started elsewhere
# (`gateway --no-bridges`) may drift a little.
MAX_SKEW_SECONDS = 5
_VERSION = "v1"
_CONTEXT = "hubzoid-identity-assertion.v1."
_MAX_TOKEN = 8192

# Header names the assertion vouches for (lowercase, as Starlette exposes them).
EMAIL_HEADERS = ("x-openwebui-user-email", "x-hubzoid-user")
GROUPS_HEADER = "x-hubzoid-groups"
SURFACE_HEADER = "x-hubzoid-surface"


@dataclass(frozen=True)
class Assertion:
    """A verified claim: who, in which groups, on which surface, for which hub."""

    hub: str
    email: str                      # normalized; "" = nobody in particular
    groups: tuple[str, ...]         # normalized, unique, sorted
    surface: str                    # normalized
    issued_at: int


def hub_key(hub_dir) -> str:
    """The hub an assertion is for: the hub folder's name, as grants name it."""
    return normalize(Path(hub_dir).name)


def canonical_groups(groups: Iterable[str] | str | None) -> tuple[str, ...]:
    if not groups:
        return ()
    if isinstance(groups, str):
        groups = groups.split(",")
    return tuple(sorted({normalize(str(g)) for g in groups if normalize(str(g))}))


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue(hub_dir, *, email: str | None = None, groups: Iterable[str] | None = (),
          surface: str, now: float | None = None, env=None) -> str:
    """Sign an assertion for `hub_dir`'s bridge. Raises secretbox.SecretKeyError
    when the deployment key is unusable."""
    from . import secretbox

    payload = {
        "v": 1,
        "hub": hub_key(hub_dir),
        "email": normalize(email or ""),
        "groups": list(canonical_groups(groups)),
        "surface": normalize(surface or ""),
        "iat": int(time.time() if now is None else now),
    }
    body = _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    return f"{_VERSION}.{body}.{secretbox.sign(Path(hub_dir), _CONTEXT + body, env)}"


def verify(hub_dir, token: str | None, *, now: float | None = None, env=None) -> Assertion | None:
    """The assertion in `token` when it is well formed, signed with the
    deployment key, for this hub and fresh; otherwise None. Never raises."""
    if hub_dir is None or not token or len(token) > _MAX_TOKEN:
        return None
    try:
        version, body, signature = token.strip().split(".")
    except ValueError:
        return None
    if version != _VERSION or not body or not signature:
        return None
    try:
        from . import secretbox

        if not secretbox.verify(Path(hub_dir), _CONTEXT + body, signature, env):
            return None
        payload = json.loads(_unb64(body))
    except Exception:  # noqa: BLE001 — an unusable key or a bad token is "no assertion"
        log.warning("identity assertion could not be checked; treating the caller as anonymous")
        return None
    if not isinstance(payload, dict) or payload.get("v") != 1:
        return None
    try:
        issued = int(payload["iat"])
        hub = str(payload["hub"])
        email = str(payload.get("email") or "")
        surface = str(payload.get("surface") or "")
        groups = payload.get("groups") or []
        if not isinstance(groups, list):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    age = (time.time() if now is None else now) - issued
    if age > VALIDITY_SECONDS or age < -MAX_SKEW_SECONDS:
        return None
    if hub != hub_key(hub_dir):
        return None
    if normalize(email) != email or normalize(surface) != surface:
        return None
    canonical = canonical_groups(groups)
    if list(canonical) != groups:
        return None
    return Assertion(hub=hub, email=email, groups=canonical, surface=surface, issued_at=issued)


def covers(assertion: Assertion, headers: Mapping[str, str]) -> bool:
    """True when the identity headers carry exactly the asserted values: each
    email header present equals the asserted email (and one is present when an
    email is asserted), the groups header lists exactly the asserted groups and
    the surface header is the asserted surface."""
    emails = [normalize(headers.get(h) or "") for h in EMAIL_HEADERS]
    present = [e for e in emails if e]
    if assertion.email:
        if not present or any(e != assertion.email for e in present):
            return False
    elif present:
        return False
    if canonical_groups(headers.get(GROUPS_HEADER)) != assertion.groups:
        return False
    return normalize(headers.get(SURFACE_HEADER) or "") == assertion.surface


def vouched(hub_dir, headers: Mapping[str, str], *, now: float | None = None) -> Assertion | None:
    """The identity a bridge may trust for a request carrying `headers`, or None
    (anonymous). Used in the web app mode only."""
    token = headers.get(HEADER.lower()) or headers.get(HEADER)
    if not token:
        return None
    assertion = verify(hub_dir, token, now=now)
    if assertion is None:
        log.info("identity assertion missing, stale or invalid; treating the caller as anonymous")
        return None
    if not covers(assertion, headers):
        log.warning("identity headers differ from their assertion; treating the caller as anonymous")
        return None
    return assertion


def identity_headers(hub_dir, *, surface: str, email: str | None = None,
                     groups: Iterable[str] | None = None, legacy: bool | None = None) -> dict[str, str]:
    """The identity headers an internal caller sends to `hub_dir`'s bridge.

    Legacy mode: exactly the 1.0.x headers. Web app mode: the same headers plus
    `X-Hubzoid-Assertion`. When the deployment key can't be used, the assertion
    is left out and the bridge treats the call as anonymous (fail closed)."""
    headers = {"X-Hubzoid-Surface": surface}
    if email:
        headers["X-OpenWebUI-User-Email"] = email
    groups = [g for g in (groups or []) if str(g).strip()]
    if groups:
        headers["X-Hubzoid-Groups"] = ",".join(groups)
    if legacy is None:
        from .appmode import is_legacy

        legacy = is_legacy(Path(hub_dir)) if hub_dir is not None else False
    if legacy or hub_dir is None:
        return headers
    try:
        headers[HEADER] = issue(hub_dir, email=email, groups=groups, surface=surface)
    except Exception:  # noqa: BLE001 — never a crash, never a wider identity
        log.warning("could not sign the caller's identity (deployment key unusable); "
                    "the bridge will treat this request as anonymous")
    return headers
