"""Installing the hub's Linear app, and keeping its token fresh.

The customer creates an OAuth application in Linear (Settings, API,
Applications) with the callback and webhook URLs ``setup`` prints, and a
workspace admin approves it with ``actor=app``, so the app acts as itself and
can be delegated issues. Linear's guide: https://linear.app/developers/agents

* ``connect`` starts one attempt: a random state (only its hash is kept) and a
  PKCE verifier (kept encrypted), valid for ten minutes, used once.
* ``callback`` checks the state, exchanges the code, then reads back the
  app user and organization and refuses a grant for any other workspace.
  The access and refresh tokens go to the secret store, never to a file.
* ``access_token`` refreshes five minutes before expiry. The refresh holds a
  database lock and re-reads the latest token first, so two workers never
  spend the same refresh token. ``invalid_grant`` blocks the connection until
  someone reconnects.

PKCE and tokens use Authlib's helpers; the token exchange is two plain POSTs.
"""
from __future__ import annotations

import hashlib
import logging
import os
import secrets
import time
from urllib.parse import urlencode

from .. import config as config_lib
from .. import store
from ..errors import ChannelError

log = logging.getLogger("hubzoid.channels.linear.oauth")

SCOPES = ("read", "write", "app:assignable", "app:mentionable")
ATTEMPT_SECONDS = 600
REFRESH_EARLY = 300


def _hash(v: str) -> str:
    return hashlib.sha256(v.encode()).hexdigest()


def _public() -> str:
    base = (os.environ.get("HUBZOID_PUBLIC_URL") or "").rstrip("/")
    if not base:
        raise ChannelError("public_url_required", "Set HUBZOID_PUBLIC_URL to the hub's HTTPS address first: Linear "
                           "returns there and sends deliveries there.", status=422)
    return base


def urls(conn) -> dict:
    base = f"{_public()}/api/hubs/{conn.hub}/channels/{conn.id}"
    return {"callback_url": f"{base}/linear/oauth/callback", "webhook_url": f"{base}/webhook"}


def setup_checklist(conn) -> dict:
    u = urls(conn)
    return {**u, "scopes": list(SCOPES), "steps": [
        "In Linear: Settings, API, Applications, Create new. Use your own app name and icon.",
        f"Set the callback URL to {u['callback_url']}.",
        f"Turn on webhooks, set the URL to {u['webhook_url']} and select Agent session events and Permission changes.",
        "Copy the client secret and the webhook signing secret into this hub with hubzoid secrets set (never paste "
        "them into a chat).",
        "Run hubzoid channels linear connect and have a workspace admin approve the app.",
    ], "installed": bool((conn.state or {}).get("linear_app_user_id"))}


def _secret_put(conn, name: str, value: str, actor: str) -> None:
    from ...secretstore import store as secret_store

    secret_store.put(conn.hub_dir, scope="channel", target=f"{conn.hub}/{conn.id}", name=name, value=value,
                     actor=actor)


def _secret_get(conn, name: str) -> str | None:
    from ...secretstore import store as secret_store

    return secret_store.value(conn.hub_dir, scope="channel", target=f"{conn.hub}/{conn.id}", name=name)


def connect(conn, *, actor: str) -> dict:
    from authlib.common.security import generate_token
    from authlib.oauth2.rfc7636 import create_s256_code_challenge

    from ... import secretbox
    from .adapter import web_url

    u = urls(conn)
    state, verifier = secrets.token_urlsafe(32), generate_token(64)
    attempt = {"id": secrets.token_hex(8), "state_hash": _hash(state), "expires": time.time() + ATTEMPT_SECONDS,
               "verifier_enc": secretbox.encrypt(conn.hub_dir, verifier), "actor": actor, "used": False}
    store.set_connection_state(conn.hub_dir, conn.hub, conn.id, linear_attempt=attempt)
    query = urlencode({"client_id": conn.settings.oauth_client_id, "redirect_uri": u["callback_url"],
                       "response_type": "code", "scope": ",".join(SCOPES), "state": state, "actor": "app",
                       "code_challenge": create_s256_code_challenge(verifier), "code_challenge_method": "S256",
                       "prompt": "consent"})
    config_lib.audit(conn.hub_dir, actor, "linear_connect_start", conn.id)
    return {"authorization_url": f"{web_url()}/oauth/authorize?{query}", "connect_attempt_id": attempt["id"],
            "expires_at": attempt["expires"]}


def _token_post(form: dict) -> dict:
    import httpx

    from .adapter import api_url

    resp = httpx.post(f"{api_url()}/oauth/token", data=form, timeout=15, follow_redirects=False)
    data = resp.json() if resp.content else {}
    if resp.status_code >= 400:
        raise ChannelError("invalid_grant" if data.get("error") == "invalid_grant" else "oauth_failed",
                           f"Linear refused the token request ({data.get('error') or resp.status_code}).",
                           status=401 if data.get("error") == "invalid_grant" else 502)
    return data


def callback(conn, *, code: str, state: str) -> dict:
    from ... import secretbox

    attempt = (conn.state or {}).get("linear_attempt") or {}
    if not attempt or not state or not secrets.compare_digest(attempt.get("state_hash", ""), _hash(state)) \
            or attempt.get("used"):
        raise ChannelError("oauth_state_invalid", "This sign-in link is not valid any more. Run connect again.",
                           status=400)
    if time.time() > float(attempt["expires"]):
        raise ChannelError("oauth_state_invalid", "This sign-in link expired. Run connect again.", status=400)
    store.set_connection_state(conn.hub_dir, conn.hub, conn.id, linear_attempt={**attempt, "used": True})
    verifier = secretbox.decrypt(conn.hub_dir, attempt["verifier_enc"]).decode()
    tokens = _token_post({"grant_type": "authorization_code", "code": code, "redirect_uri": urls(conn)["callback_url"],
                          "client_id": conn.settings.oauth_client_id, "client_secret": conn.secret("client_secret_ref"),
                          "code_verifier": verifier})
    who = _whoami(tokens["access_token"])
    org = (who.get("organization") or {}).get("id")
    if org != conn.settings.organization_id:
        _revoke(tokens["access_token"])
        raise ChannelError("workspace_mismatch", f"The app was approved in another workspace "
                           f"({(who.get('organization') or {}).get('name')}). Nothing was saved.", status=409)
    _save_tokens(conn, tokens, actor=attempt.get("actor") or "linear-connect")
    store.set_connection_state(conn.hub_dir, conn.hub, conn.id, linear_app_user_id=(who.get("viewer") or {}).get("id"),
                               linear_org_name=(who.get("organization") or {}).get("name"),
                               linear_blocked=False)
    config_lib.audit(conn.hub_dir, attempt.get("actor") or "system", "linear_connected", conn.id)
    return {"state": "connected", "organization": (who.get("organization") or {}).get("name"),
            "app_user_id": (who.get("viewer") or {}).get("id"), "scopes": tokens.get("scope"),
            "expires_at": time.time() + int(tokens.get("expires_in") or 0)}


def _whoami(token: str) -> dict:
    import httpx

    from .adapter import api_url

    resp = httpx.post(f"{api_url()}/graphql", json={"query": "query { viewer { id name } organization { id name } }"},
                      headers={"Authorization": f"Bearer {token}"}, timeout=15, follow_redirects=False)
    data = resp.json() if resp.content else {}
    if resp.status_code >= 400 or data.get("errors"):
        raise ChannelError("oauth_failed", "Linear did not say which workspace approved the app.", status=502)
    return data.get("data") or {}


def _revoke(token: str) -> None:
    import httpx

    from .adapter import api_url

    try:
        httpx.post(f"{api_url()}/oauth/revoke", data={"token": token}, timeout=10)
    except Exception:  # noqa: BLE001 - best effort
        pass


def _save_tokens(conn, tokens: dict, *, actor: str) -> None:
    _secret_put(conn, "access-token", tokens["access_token"], actor)
    if tokens.get("refresh_token"):
        _secret_put(conn, "refresh-token", tokens["refresh_token"], actor)
    scope = tokens.get("scope") or ""
    scopes = scope if isinstance(scope, list) else [s for s in str(scope).replace(" ", ",").split(",") if s]
    rev = int((conn.state or {}).get("linear_credential_revision") or 0) + 1
    store.set_connection_state(conn.hub_dir, conn.hub, conn.id,
                               linear_expires_at=time.time() + int(tokens.get("expires_in") or 0),
                               linear_scopes=scopes, linear_credential_revision=rev)


def _take_lease(conn) -> bool:
    """A short write transaction: become the one worker refreshing, unless
    another holds a live lease."""
    import json

    from sqlalchemy import text

    with store.locked(conn.hub_dir, f"linear-refresh:{conn.hub}:{conn.id}") as c:
        row = c.execute(text("SELECT state_json FROM hz_channel_connections WHERE hub=:h AND id=:i"),
                        {"h": conn.hub, "i": conn.id}).first()
        state = json.loads(row[0] or "{}") if row else {}
        if float(state.get("linear_expires_at") or 0) > time.time() + REFRESH_EARLY:
            return False                       # someone already refreshed
        if float(state.get("linear_refresh_lease") or 0) > time.time():
            return False                       # someone is refreshing now
        state["linear_refresh_lease"] = time.time() + 30
        c.execute(text("UPDATE hz_channel_connections SET state_json=:s WHERE hub=:h AND id=:i"),
                  {"s": store.dumps(state), "h": conn.hub, "i": conn.id})
    return True


def access_token(conn) -> str:
    """The current access token, refreshed first when it is about to expire.
    One worker refreshes (a lease on the connection row) and the others wait
    for its result, so a rotating refresh token is spent once."""
    state = conn.state or {}
    if state.get("linear_blocked"):
        raise ChannelError("reauth_required", "The Linear app's grant was refused. Reconnect it.", status=503)
    token = _secret_get(conn, "access-token")
    if not token:
        raise ChannelError("credential_missing", "The Linear app is not connected. Run hubzoid channels linear connect.",
                           status=503)
    expires = float(state.get("linear_expires_at") or 0)
    if not expires or time.time() < expires - REFRESH_EARLY:
        return token
    if not _take_lease(conn):
        for _ in range(20):                    # another worker refreshes: use its token
            current = config_lib.active(conn.hub_dir, conn.id)
            if float((current.state or {}).get("linear_expires_at") or 0) > time.time() + REFRESH_EARLY:
                conn.state = current.state
                return _secret_get(current, "access-token") or token
            time.sleep(0.5)
        return token
    try:
        refresh = _secret_get(conn, "refresh-token")
        if not refresh:
            return token
        try:
            tokens = _token_post({"grant_type": "refresh_token", "refresh_token": refresh,
                                  "client_id": conn.settings.oauth_client_id,
                                  "client_secret": conn.secret("client_secret_ref")})
        except ChannelError as exc:
            if exc.code == "invalid_grant":
                store.set_connection_state(conn.hub_dir, conn.hub, conn.id, linear_blocked=True)
                raise ChannelError("reauth_required", "Linear refused the refresh. Reconnect the app.",
                                   status=503) from None
            raise
        _save_tokens(conn, tokens, actor="linear-refresh")
    finally:
        store.set_connection_state(conn.hub_dir, conn.hub, conn.id, linear_refresh_lease=0)
    conn.state = config_lib.active(conn.hub_dir, conn.id).state
    return tokens["access_token"]


def disconnect(conn, *, actor: str) -> dict:
    from ...secretstore import store as secret_store

    token = _secret_get(conn, "access-token")
    if token:
        _revoke(token)
    for name in ("access-token", "refresh-token"):
        try:
            secret_store.delete(conn.hub_dir, scope="channel", target=f"{conn.hub}/{conn.id}", name=name, actor=actor)
        except Exception:  # noqa: BLE001 - already gone
            pass
    store.set_connection_state(conn.hub_dir, conn.hub, conn.id, linear_app_user_id=None, linear_expires_at=None,
                               linear_blocked=True)
    config_lib.audit(conn.hub_dir, actor, "linear_disconnected", conn.id)
    return {"state": "disconnected"}
