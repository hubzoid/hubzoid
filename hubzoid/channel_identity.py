"""Who a channel's verified sender is, as the bridge should see them.

Slack verifies a sender's profile email server-side (``users.info``), so the
Slack adapter can say who is asking. What that email means depends on the mode:

  * Open WebUI mode (1.0.x, unchanged): the email is forwarded as it is
    and the bridge maps it to the person's Open WebUI groups.
  * Web app mode: the email must belong to a Hubzoid account that may sign in,
    active and not blocked. The bridge then applies that account's Hubzoid
    groups and grants (``access.effective_groups``, ``GrantStore.can``). An
    email without such an account is sent as nobody: an agent whose access is
    managed in the Console refuses it, any other agent answers without
    restricted tools. With sign-in off (local mode) there are no accounts to
    map to, so the verified email is used as it is.

Any lookup error sends nobody (fail closed).
"""
from __future__ import annotations

import logging
from pathlib import Path

from .access.identity import normalize

log = logging.getLogger("hubzoid.channel_identity")


def account_email(hub_dir, email: str | None, *, env=None) -> str | None:
    """The identity to send for a channel-verified `email`, or None (anonymous)."""
    if not email or not str(email).strip():
        return None
    from . import appmode

    hub_dir = Path(hub_dir)
    try:
        if appmode.is_openwebui(hub_dir, env):
            return email
        sign_in = appmode.auth_enabled(hub_dir, env)
    except Exception:  # noqa: BLE001
        log.warning("channel identity: mode unreadable; sending the sender as nobody")
        return None
    wanted = normalize(email)
    try:
        from sqlalchemy import text

        from .access import store_for

        gs = store_for(hub_dir)
        if gs.is_suspended(wanted):
            return None
        with gs.engine.connect() as conn:
            row = conn.execute(
                text("SELECT email, status FROM hz_users WHERE lower(email) = :e"),
                {"e": wanted},
            ).fetchone()
    except Exception:  # noqa: BLE001 — fail closed
        log.warning("channel identity: account lookup failed; sending the sender as nobody")
        return None
    if row is None:
        return wanted if not sign_in else None
    account, status = row
    return normalize(account) if status == "active" else None
