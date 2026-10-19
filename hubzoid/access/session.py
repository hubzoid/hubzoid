# Hubzoid access management. Apache-2.0 licensed like the rest of the repository.
"""Who is asking, verified server-side, and the same-origin rule for mutations.

Every caller that needs a person's verified email (the Console API, artifact
pages, the connection pages, MCP consent) uses these, never a client-sent
identity header. The session checked is Hubzoid's own (``hubzoid.auth``).
With sign-in off, every request is the local owner, ``admin@localhost``.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import HTTPException, Request

from .. import deployment
from .identity import normalize

log = logging.getLogger("hubzoid.portal")


def require_same_origin(request: Request) -> None:
    """Reject a cross-site mutation. The session cookie is ambient, so a POST
    must come from our own origin: an Origin (or Referer) whose host equals
    the request's Host or one of the deployment's origins
    (``appmode.allowed_origins``; a deployment may answer on two public
    names)."""
    from urllib.parse import urlparse

    from .. import appmode

    origin = request.headers.get("origin") or request.headers.get("referer") or ""
    parsed = urlparse(origin)
    # Require a real http/https origin with an authority — 'null', an opaque
    # origin, or a missing header is rejected (it can't be proven same-origin).
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(
            status_code=403, detail="missing or invalid Origin on a mutation"
        )
    allowed = appmode.allowed_origins()
    if allowed and appmode.normalize_origin(origin) in allowed:
        return
    req_host = request.headers.get("host", "")
    if not req_host or parsed.netloc != req_host:
        raise HTTPException(status_code=403, detail="cross-origin request refused")


LOCAL_OWNER = "admin@localhost"


def configured_owner(hub_dir: Path) -> str:
    """The deployment's configured initial owner: the one account Hubzoid
    provisions on its first verified sign-in.

    ``HUBZOID_ADMIN_EMAIL``, then the gateway's recorded owner. With sign-in
    off every request is the local owner, ``admin@localhost``. The Open WebUI
    names for the owner stop a start until renamed (``appmode.RETIRED``)."""
    from .. import appmode

    if not appmode.auth_enabled(hub_dir):
        return LOCAL_OWNER
    owner = (os.environ.get("HUBZOID_ADMIN_EMAIL") or "").strip().lower()
    if not owner:
        try:
            owner = (deployment.read(hub_dir).get("owner") or "").strip().lower()
        except (OSError, ValueError, KeyError):
            owner = ""
    return owner


def verified_email(request: Request, hub_dir: Path | None = None, *,
                   bearer: bool = False) -> str:
    """The viewer's verified email, or '' when nobody is signed in. Never
    trusts a client-sent identity header.

    A verified viewer is recorded on their access identity, and the configured
    owner, signed in as an administrator, gets the owner's grants once.

    With ``bearer``, a request without the session cookie may instead carry the
    chat app's own credential as ``Authorization: Bearer <token>``. Only
    read-only checks pass it, because a bearer credential is not ambient like
    a cookie."""
    who = verified_person(request, hub_dir, bearer=bearer)
    return who[1] if who else ""


def verified_person(request: Request, hub_dir: Path | None = None, *,
                    bearer: bool = False) -> tuple[str, str] | None:
    """``(account id, email)`` of the signed-in viewer, from a live check of
    their session, or None when nobody is signed in. The account id is
    Hubzoid's, so a new account that reuses an email is a different person.
    Never trusts a client-sent header."""
    return _hubzoid_verified(request, hub_dir, bearer=bearer)


def _hubzoid_verified(request: Request, hub_dir: Path | None, *,
                      bearer: bool) -> tuple[str, str] | None:
    if hub_dir is None:
        return None
    from .. import auth
    from ..auth import sessions as sessionlib
    from ..auth import users

    try:
        user = auth.current_user(request, hub_dir)
        if user is None and bearer and not request.cookies.get(sessionlib.SESSION_COOKIE):
            scheme, _, value = (request.headers.get("authorization") or "").partition(" ")
            if scheme.lower() == "bearer" and value.strip():
                user = sessionlib.resolve_token(hub_dir, value.strip())
    except HTTPException as exc:
        if exc.status_code >= 500:
            raise HTTPException(503, "Could not verify the account service. Try again shortly.")
        raise
    except Exception:  # noqa: BLE001
        log.warning("portal: session verification failed")
        raise HTTPException(503, "Could not verify the account service. Try again shortly.")
    if user is None:
        return None
    email = normalize(user.email)
    try:
        users.on_sign_in(Path(hub_dir), {"id": user.id, "email": email, "name": user.name,
                                         "role": user.role, "status": "active"})
    except Exception:  # noqa: BLE001
        log.warning("portal: recording the verified account failed")
        raise HTTPException(503, "Could not verify the account service. Try again shortly.")
    return (str(user.id), email)
