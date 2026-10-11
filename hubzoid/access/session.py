# Hubzoid access management. Apache-2.0 licensed like the rest of the repository.
"""Who is asking, verified server-side, and the same-origin rule for mutations.

Every caller that needs a person's verified email (the Console API, artifact
pages, the connection pages, MCP consent) uses these, never a client-sent
identity header. Which session is checked depends on the UI mode
(``hubzoid.appmode``):

  * default (``hubzoid``): the Hubzoid session (``hubzoid.auth``). With
    sign-in off, every request is the local owner, ``admin@localhost``.
  * Open WebUI mode (``openwebui``): the Open WebUI session, validated by
    Open WebUI, as in 1.0.x.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import HTTPException, Request

from .. import deployment
from . import store_for
from .identity import normalize

log = logging.getLogger("hubzoid.portal")


def _truthy_env(name: str) -> bool:
    return (os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


def require_same_origin(request: Request) -> None:
    """Reject a cross-site mutation. The session cookie is ambient, so a POST
    must come from our own origin: an Origin (or Referer) whose host equals
    the request's Host or, in the default mode, one of the deployment's
    origins (``appmode.allowed_origins``; a deployment may answer on two
    public names). Open WebUI mode keeps the 1.0.x rule exactly."""
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
    if not appmode.is_openwebui():
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

    Default mode: ``HUBZOID_ADMIN_EMAIL``, ``WEBUI_ADMIN_EMAIL``,
    ``HUBZOID_GATEWAY_ADMIN_EMAIL``, then the gateway's recorded owner. With
    sign-in off every request is the local owner, ``admin@localhost``.

    Open WebUI mode keeps 1.0.x: the gateway service account or
    ``WEBUI_ADMIN_EMAIL``, the recorded owner, and ``admin@localhost`` for the
    local quickstart (authentication off, no deployment)."""
    from .. import appmode

    if appmode.is_openwebui(hub_dir):
        return _legacy_configured_owner(hub_dir)
    if not appmode.auth_enabled(hub_dir):
        return LOCAL_OWNER
    owner = (os.environ.get("HUBZOID_ADMIN_EMAIL") or os.environ.get("WEBUI_ADMIN_EMAIL")
             or os.environ.get("HUBZOID_GATEWAY_ADMIN_EMAIL") or "").strip().lower()
    if not owner:
        try:
            owner = (deployment.read(hub_dir).get("owner") or "").strip().lower()
        except (OSError, ValueError, KeyError):
            owner = ""
    return owner


def _legacy_configured_owner(hub_dir: Path) -> str:
    owner = (os.environ.get("HUBZOID_GATEWAY_ADMIN_EMAIL")
             or os.environ.get("WEBUI_ADMIN_EMAIL") or "").strip().lower()
    if not owner:
        # A bridge run as its own service (gateway --no-bridges) does not see
        # the gateway's environment; the gateway records the owner here.
        try:
            owner = (deployment.read(hub_dir).get("owner") or "").strip().lower()
        except (OSError, ValueError, KeyError):
            owner = ""
    if not _truthy_env("WEBUI_AUTH") and not deployment.read(hub_dir):
        owner = LOCAL_OWNER
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
    their session, or None when nobody is signed in. The account id is Open
    WebUI's in Open WebUI mode and Hubzoid's otherwise, so a new account that
    reuses an email is a different person. Never trusts a client-sent header."""
    from .. import appmode

    if appmode.is_openwebui(hub_dir):
        return _owui_verified(request, hub_dir, bearer=bearer)
    return _hubzoid_verified(request, hub_dir, bearer=bearer)


def bind_owui_account(hub_dir: Path, email: str, owui_id: str | None,
                      display: str | None = None) -> None:
    """Record the verified Open WebUI account behind ``email``. When a new
    account has taken the email over, the earlier account's personal
    connections are removed with its grants (``upsert_identity``)."""
    gs = store_for(hub_dir)
    before = (gs.identity(email) or {}).get("owui_id") if owui_id else None
    gs.upsert_identity(email=email, owui_id=owui_id, display=display)
    if before and before != owui_id:
        try:
            from ..connectors import tokens

            tokens.drop_user(Path(hub_dir), str(before))
        except Exception:  # noqa: BLE001 — unusable anyway: keyed by the old account
            log.warning("access: connections of a replaced account were not removed")


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


def _owui_verified(request: Request, hub_dir: Path | None, *,
                   bearer: bool) -> tuple[str, str] | None:
    """Open WebUI mode: validate the Open WebUI session cookie with Open WebUI."""
    token = request.cookies.get("token") or ""
    if not token and bearer:
        scheme, _, value = (request.headers.get("authorization") or "").partition(" ")
        if scheme.lower() == "bearer":
            token = value.strip()
    if not token:
        return None
    base = (
        deployment.owui_url(hub_dir)
        if hub_dir
        else (os.environ.get("OWUI_INTERNAL_URL") or os.environ.get("WEBUI_URL"))
    )
    if not base:
        return None
    try:
        import httpx

        r = httpx.get(
            f"{base.rstrip('/')}/api/v1/auths/",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5.0,
        )
        if r.status_code >= 500:
            raise HTTPException(503, "The account service is unavailable. Try again shortly.")
        if r.status_code == 200:
            user = r.json()
            if user.get("role") == "pending":
                return None
            email = normalize(user.get("email", ""))
            account = str(user.get("id") or "")
            if not email or not account:
                return None
            if hub_dir:
                bind_owui_account(hub_dir, email, account, user.get("name"))
                # Authentication remains in OWUI. Only the configured owner is
                # provisioned, once; an arbitrary admin/member cannot self-promote.
                owner = configured_owner(hub_dir)
                if user.get("role") == "admin" and email == owner:
                    for h in deployment.hubs(hub_dir):
                        path = Path(h["path"])
                        store_for(path).provision_owner(email, h["key"])
            return (account, email)
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        log.warning("portal: OWUI session verification failed")
        raise HTTPException(503, "Could not verify the account service. Try again shortly.")
    return None
