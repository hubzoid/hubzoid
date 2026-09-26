# Hubzoid access management. Apache-2.0 licensed like the rest of the repository.
"""Who is asking, verified server-side: the Open WebUI session behind a Console
or connection-page request, and the same-origin rule for mutations.

Every caller that needs a person's verified email (the Console API, the
connection pages) uses these, never a client-sent identity header.
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
    must come from our own origin (Origin or Referer host == request host)."""
    from urllib.parse import urlparse

    origin = request.headers.get("origin") or request.headers.get("referer") or ""
    parsed = urlparse(origin)
    # Require a real http/https origin with an authority — 'null', an opaque
    # origin, or a missing header is rejected (it can't be proven same-origin).
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(
            status_code=403, detail="missing or invalid Origin on a mutation"
        )
    req_host = request.headers.get("host", "")
    if not req_host or parsed.netloc != req_host:
        raise HTTPException(status_code=403, detail="cross-origin request refused")


LOCAL_OWNER = "admin@localhost"


def configured_owner(hub_dir: Path) -> str:
    """The deployment's configured initial owner: the one account Hubzoid
    provisions on its first verified sign-in. Local quickstart (authentication
    off, no deployment) has exactly one account, `admin@localhost`."""
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
    """Validate the viewer's OWUI session cookie server-side and return the
    verified email, or '' — never trusting a client-sent identity header.

    With ``bearer``, a request without the cookie may instead carry the chat
    app's own credential as ``Authorization: Bearer <token>`` (a session token
    or an API key), validated by Open WebUI the same way. Only read-only checks
    pass it, because a bearer credential is not ambient like a cookie."""
    token = request.cookies.get("token") or ""
    if not token and bearer:
        scheme, _, value = (request.headers.get("authorization") or "").partition(" ")
        if scheme.lower() == "bearer":
            token = value.strip()
    if not token:
        return ""
    base = (
        deployment.owui_url(hub_dir)
        if hub_dir
        else (os.environ.get("OWUI_INTERNAL_URL") or os.environ.get("WEBUI_URL"))
    )
    if not base:
        return ""
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
                return ""
            email = normalize(user.get("email", ""))
            if hub_dir and email:
                store_for(hub_dir).upsert_identity(
                    email=email, owui_id=user.get("id"), display=user.get("name")
                )
                # Authentication remains in OWUI. Only the configured owner is
                # provisioned, once; an arbitrary admin/member cannot self-promote.
                owner = configured_owner(hub_dir)
                if user.get("role") == "admin" and email == owner:
                    for h in deployment.hubs(hub_dir):
                        path = Path(h["path"])
                        store_for(path).provision_owner(
                            email, h["key"], fresh=(path / ".hubzoid" / "fresh-install").exists()
                        )
            return email
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        log.warning("portal: OWUI session verification failed")
        raise HTTPException(503, "Could not verify the account service. Try again shortly.")
    return ""
