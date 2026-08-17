"""Keep a user's per-tool OAuth token fresh.

OWUI refreshes a token only when it runs the tool itself, which never happens
for a Hubzoid model - so a connected token would die at expiry (~1h to a day
depending on the provider). This module refreshes it the standard way (a
``grant_type=refresh_token`` POST to the token endpoint, matching OWUI's own
``_perform_token_refresh``) and writes the fresh token back to the same
``oauth_session`` row, so OWUI and the bridge stay in sync (single source of
truth; no second copy to reconcile).

Single entry point: ``access_token_for`` returns a valid access token or None.
None means "connected but not usable now" - no refresh token, or the refresh
failed (revoked / refresh token itself expired); the caller drops that server
and the user reconnects. Best-effort throughout: never raises into a chat turn.
The refresh POST is synchronous and only runs on the rare expiry boundary.
"""
from __future__ import annotations

import logging
import time

from .access import owui_client_info as client_info
from .access import owui_oauth_tokens as tokens

log = logging.getLogger("hubzoid.owui_refresh")

_SKEW = 60  # refresh this many seconds before the token actually expires


def _expired(token: dict) -> bool:
    exp = token.get("expires_at")
    if not exp:
        return False
    try:
        return time.time() >= float(exp) - _SKEW
    except (TypeError, ValueError):
        return False


def _expires_at(new: dict) -> int:
    exp = new.get("expires_at")
    if exp:
        try:
            return int(exp)
        except (TypeError, ValueError):
            pass
    try:
        return int(time.time()) + int(new.get("expires_in", 3600))
    except (TypeError, ValueError):
        return int(time.time()) + 3600


def _post_refresh(ci: dict, refresh_token: str, scope: str | None) -> dict | None:
    import httpx

    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": ci["client_id"],
    }
    if ci.get("client_secret"):
        data["client_secret"] = ci["client_secret"]
    if scope or ci.get("scope"):
        data["scope"] = scope or ci["scope"]
    if ci.get("resource"):
        data["resource"] = ci["resource"]
    try:
        r = httpx.post(
            ci["token_endpoint"], data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=15.0,
        )
    except Exception:  # noqa: BLE001
        log.warning("owui-refresh: request to %s failed", ci.get("token_endpoint"), exc_info=True)
        return None
    if r.status_code != 200:
        log.info("owui-refresh: token endpoint returned %s (reconnect likely needed)", r.status_code)
        return None
    try:
        body = r.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) and body.get("access_token") else None


def access_token_for(hub_dir, user_id: str, server_id: str, conn: dict | None = None) -> str | None:
    """A currently-valid access token for the caller + server, refreshing the
    stored one if it has expired. None when there is no usable token (not
    connected, no refresh token, or refresh failed)."""
    sess = tokens.read_session(hub_dir, user_id, server_id)
    if not sess:
        return None
    token = sess["token"]
    if not _expired(token):
        return token.get("access_token") or None

    refresh_token = token.get("refresh_token")
    if not refresh_token:
        log.info("owui-refresh: %r expired for %s, no refresh token; reconnect needed", server_id, user_id)
        return None
    ci = client_info.client_info(hub_dir, server_id, conn)
    if not ci:
        log.info("owui-refresh: no client info for %r; cannot refresh", server_id)
        return None
    new = _post_refresh(ci, refresh_token, token.get("scope"))
    if not new:
        return None

    merged = {**token, **new}
    if not new.get("refresh_token"):
        merged["refresh_token"] = refresh_token  # non-rotating provider
    merged["expires_at"] = _expires_at(new)
    tokens.write_session(hub_dir, sess["id"], merged)
    log.info("owui-refresh: refreshed %r for %s", server_id, user_id)
    return merged.get("access_token")
