"""Gmail and Microsoft 365 consent, and keeping the mailbox token fresh.

Both use the customer's own OAuth app (a Google Cloud OAuth client, a
Microsoft Entra app). ``connect`` returns a consent link with a one-time
state and PKCE; ``callback`` exchanges the code, reads back which mailbox
consented and refuses any other. The token set is kept in the connection's
``oauth_ref`` secret slot, never in a file or a response. ``access_token``
refreshes it shortly before it expires, one worker at a time.

Scopes are the least each mode needs:

* Gmail: ``gmail.readonly`` to read new mail, plus ``gmail.compose`` for real
  drafts (which also lets the provider send, but Hubzoid still sends only an
  approved message), or ``gmail.send`` for approve-only sending.
* Microsoft 365: ``offline_access``, ``User.Read``, ``Mail.ReadWrite`` (read,
  and drafts) and ``Mail.Send``.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import time
from urllib.parse import urlencode

from .. import config as config_lib
from .. import store
from ..errors import ChannelError

ATTEMPT_SECONDS = 600
REFRESH_EARLY = 300


def _env(name: str, default: str) -> str:
    return (os.environ.get(name) or default).rstrip("/")


def endpoints(conn) -> dict:
    s = conn.settings
    if s.transport == "gmail":
        return {"auth": _env("HUBZOID_GOOGLE_AUTH_URL", "https://accounts.google.com/o/oauth2/v2/auth"),
                "token": _env("HUBZOID_GOOGLE_TOKEN_URL", "https://oauth2.googleapis.com/token"),
                "client_id": s.gmail.client_id, "secret_field": ("gmail", "client_secret_ref"),
                "token_field": ("gmail", "oauth_ref")}
    m = s.microsoft365
    login = _env("HUBZOID_MS_LOGIN_URL", "https://login.microsoftonline.com")
    return {"auth": f"{login}/{m.tenant_id}/oauth2/v2.0/authorize", "token": f"{login}/{m.tenant_id}/oauth2/v2.0/token",
            "client_id": m.app_id, "secret_field": ("microsoft365", "client_secret_ref"),
            "token_field": ("microsoft365", "oauth_ref")}


def scopes(conn, mode: str) -> list[str]:
    if conn.settings.transport == "gmail":
        base = ["https://www.googleapis.com/auth/gmail.readonly"]
        if mode == "draft":
            base.append("https://www.googleapis.com/auth/gmail.compose")
        elif mode == "approve":
            base.append("https://www.googleapis.com/auth/gmail.send")
        return base
    return ["offline_access", "User.Read", "Mail.ReadWrite", "Mail.Send"]


def _secret(conn, section: str, field: str) -> str:
    from ...secretstore import resolve

    ref = getattr(getattr(conn.settings, section), field)
    try:
        return resolve(ref, conn.hub_dir)
    except Exception as exc:  # noqa: BLE001
        raise ChannelError("credential_missing", f"Set the secret {ref} first (hubzoid secrets set).",
                           status=503) from exc


def _ref_parts(ref: str) -> tuple[str, str, str]:
    from ...secretstore import parse

    r = parse(ref)
    if r.provider != "secret":
        raise ChannelError("invalid_config", "oauth_ref must be a secret: reference the hub can write.", status=422)
    return r.scope, r.target, r.name


def _redirect(conn) -> str:
    base = (os.environ.get("HUBZOID_PUBLIC_URL") or "").rstrip("/")
    if not base:
        raise ChannelError("public_url_required", "Set HUBZOID_PUBLIC_URL first: the consent page returns there.",
                           status=422)
    return f"{base}/api/hubs/{conn.hub}/channels/{conn.id}/email/oauth/callback"


def connect(conn, *, actor: str, mode: str = "approve") -> dict:
    from authlib.common.security import generate_token
    from authlib.oauth2.rfc7636 import create_s256_code_challenge

    from ... import secretbox

    if conn.settings.transport == "imap":
        raise ChannelError("validation_failed", "IMAP mailboxes use a password secret, not a consent link.", status=422)
    ep = endpoints(conn)
    state, verifier = secrets.token_urlsafe(32), generate_token(64)
    attempt = {"id": secrets.token_hex(8), "state_hash": hashlib.sha256(state.encode()).hexdigest(),
               "expires": time.time() + ATTEMPT_SECONDS, "verifier_enc": secretbox.encrypt(conn.hub_dir, verifier),
               "actor": actor, "used": False, "mode": mode}
    store.set_connection_state(conn.hub_dir, conn.hub, conn.id, email_attempt=attempt)
    q = {"client_id": ep["client_id"], "redirect_uri": _redirect(conn), "response_type": "code",
         "scope": " ".join(scopes(conn, mode)), "state": state, "code_challenge": create_s256_code_challenge(verifier),
         "code_challenge_method": "S256"}
    if conn.settings.transport == "gmail":
        q.update(access_type="offline", prompt="consent", login_hint=conn.settings.mailbox)
    else:
        q.update(login_hint=conn.settings.mailbox)
    config_lib.audit(conn.hub_dir, actor, "email_connect_start", conn.id)
    return {"attempt_id": attempt["id"], "authorization_url": f"{ep['auth']}?{urlencode(q)}",
            "expires_in": ATTEMPT_SECONDS, "scopes": scopes(conn, mode)}


def _post(url: str, form: dict) -> dict:
    from . import transports

    resp = transports.http().post(url, data=form, timeout=15)
    data = resp.json() if resp.content else {}
    if resp.status_code >= 400:
        code = "invalid_grant" if data.get("error") == "invalid_grant" else "oauth_failed"
        raise ChannelError(code, f"The provider refused the token request ({data.get('error') or resp.status_code}).",
                           status=401 if code == "invalid_grant" else 502)
    return data


def callback(conn, *, code: str, state: str) -> dict:
    from ... import secretbox
    from . import transports

    def take(current: dict):
        attempt = current.get("email_attempt") or {}
        if not attempt or attempt.get("used") or not state or not secrets.compare_digest(
                attempt.get("state_hash", ""), hashlib.sha256(state.encode()).hexdigest()):
            raise ChannelError("oauth_state_invalid", "This consent link is not valid any more. Run connect again.",
                               status=400)
        if time.time() > float(attempt["expires"]):
            raise ChannelError("oauth_state_invalid", "This consent link expired. Run connect again.", status=400)
        return {"email_attempt": {**attempt, "used": True}}, attempt

    attempt = store.swap_connection_state(conn.hub_dir, conn.hub, conn.id, take)
    ep = endpoints(conn)
    tokens = _post(ep["token"], {"grant_type": "authorization_code", "code": code, "redirect_uri": _redirect(conn),
                                 "client_id": ep["client_id"], "client_secret": _secret(conn, *ep["secret_field"]),
                                 "code_verifier": secretbox.decrypt(conn.hub_dir, attempt["verifier_enc"]).decode()})
    tokens["expires_at"] = time.time() + int(tokens.get("expires_in") or 3600)
    who = transports.for_conn(conn).whoami(tokens["access_token"])
    if (who or "").lower() != conn.settings.mailbox.lower():
        raise ChannelError("mailbox_mismatch", f"Consent was given for {who}, not {conn.settings.mailbox}. Nothing was "
                           "saved.", status=409)
    _save(conn, tokens, actor=attempt.get("actor") or "email-connect")
    store.set_connection_state(conn.hub_dir, conn.hub, conn.id, email_scopes=str(tokens.get("scope") or "").split(),
                               email_blocked=False)
    config_lib.audit(conn.hub_dir, attempt.get("actor") or "system", "email_connected", conn.id)
    return {"state": "connected", "mailbox": who, "scopes": str(tokens.get("scope") or "").split()}


def _save(conn, tokens: dict, *, actor: str) -> None:
    from ...secretstore import store as secret_store

    section, field = endpoints(conn)["token_field"]
    scope, target, name = _ref_parts(getattr(getattr(conn.settings, section), field))
    keep = {k: tokens[k] for k in ("access_token", "refresh_token", "expires_at", "scope") if k in tokens}
    if "refresh_token" not in keep:
        try:
            old = json.loads(secret_store.value(conn.hub_dir, scope=scope, target=target, name=name) or "{}")
            if old.get("refresh_token"):
                keep["refresh_token"] = old["refresh_token"]
        except ValueError:
            pass
    secret_store.put(conn.hub_dir, scope=scope, target=target, name=name, value=json.dumps(keep), actor=actor)


def _tokens(conn) -> dict:
    from ...secretstore import store as secret_store

    section, field = endpoints(conn)["token_field"]
    scope, target, name = _ref_parts(getattr(getattr(conn.settings, section), field))
    raw = secret_store.value(conn.hub_dir, scope=scope, target=target, name=name)
    if not raw:
        raise ChannelError("auth_required", "The mailbox is not connected. Run hubzoid channels email connect.",
                           status=503)
    try:
        return json.loads(raw)
    except ValueError:
        raise ChannelError("auth_required", "The stored token set is unreadable. Connect again.", status=503) from None


def current_token(conn) -> str | None:
    """The stored access token while it is still valid, without renewing it
    (doctor must not change credentials)."""
    try:
        tokens = _tokens(conn)
    except ChannelError:
        return None
    if float(tokens.get("expires_at") or 0) <= time.time():
        return None
    return tokens.get("access_token")


def access_token(conn) -> str:
    if (conn.state or {}).get("email_blocked"):
        raise ChannelError("auth_revoked", "The mailbox consent was withdrawn. Connect again.", status=503)
    tokens = _tokens(conn)
    if float(tokens.get("expires_at") or 0) > time.time() + REFRESH_EARLY:
        return tokens["access_token"]
    owner = secrets.token_hex(8)

    def take(state: dict):
        lease = state.get("email_refresh_lease") or {}
        if isinstance(lease, dict) and float(lease.get("until") or 0) > time.time():
            return None, False
        return {"email_refresh_lease": {"owner": owner, "until": time.time() + 30}}, True

    mine = store.swap_connection_state(conn.hub_dir, conn.hub, conn.id, take)
    if not mine:
        for _ in range(20):
            time.sleep(0.5)
            tokens = _tokens(conn)
            if float(tokens.get("expires_at") or 0) > time.time() + REFRESH_EARLY:
                return tokens["access_token"]
        return tokens["access_token"]
    try:
        tokens = _tokens(conn)       # another worker may have finished meanwhile
        if float(tokens.get("expires_at") or 0) > time.time() + REFRESH_EARLY:
            return tokens["access_token"]
        if not tokens.get("refresh_token"):
            raise ChannelError("auth_required", "The mailbox token expired and cannot renew. Connect again.",
                               status=503)
        ep = endpoints(conn)
        try:
            fresh = _post(ep["token"], {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"],
                                        "client_id": ep["client_id"],
                                        "client_secret": _secret(conn, *ep["secret_field"])})
        except ChannelError as exc:
            if exc.code == "invalid_grant":
                store.set_connection_state(conn.hub_dir, conn.hub, conn.id, email_blocked=True)
                raise ChannelError("auth_revoked", "The provider refused the refresh. Connect again.",
                                   status=503) from None
            raise
        fresh["expires_at"] = time.time() + int(fresh.get("expires_in") or 3600)
        _save(conn, fresh, actor="email-refresh")
        return fresh["access_token"]
    finally:
        def release(state: dict):
            lease = state.get("email_refresh_lease") or {}
            return ({"email_refresh_lease": {}} if isinstance(lease, dict) and lease.get("owner") == owner
                    else None), None

        store.swap_connection_state(conn.hub_dir, conn.hub, conn.id, release)
