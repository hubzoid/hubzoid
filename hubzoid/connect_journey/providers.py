"""Where a connection is actually made and checked.

A provider answers three questions for one person and one app, always from its
own records and never from browser callback parameters:

  * ``status``: is the person connected now (``connected|none|expired``)?
  * ``begin``: where does the browser go to authorize?
  * ``verify``: did *this* journey connect (``connected|pending|failed``)?

Two providers exist. Each app resolves to exactly one of them per hub
(:func:`for_app`), so a person never ends up with two connections for one app.

  * :class:`OwuiMcpProvider`, the default: an MCP tool server registered in Open
    WebUI with OAuth 2.1. Open WebUI runs the authorization and stores the
    person's token in ``oauth_session``. Verified by a session row created after
    the journey started whose token is usable now.
  * :class:`ComposioProvider`: a sanctioned app in ``CONNECTIONS``. Composio
    holds the token. Verified by the connected account this journey's link
    created being ACTIVE for this person and app.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from ..access.identity import normalize

log = logging.getLogger("hubzoid.connect")


class JourneyError(Exception):
    """A journey cannot proceed. ``message`` is safe to show the person."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class Provider(Protocol):
    name: str

    def status(self, subject: str, app: str) -> str: ...

    def begin(self, journey: dict) -> str: ...

    def verify(self, journey: dict) -> str: ...

    def cleanup_duplicates(self, journey: dict) -> None: ...


_LABELS = {"gmail": "Gmail", "github": "GitHub", "google_calendar": "Google Calendar",
           "google_drive": "Google Drive", "googlecalendar": "Google Calendar",
           "googledrive": "Google Drive", "linear": "Linear", "notion": "Notion",
           "slack": "Slack", "jira": "Jira", "odoo": "Odoo"}


def label(app: str) -> str:
    """A display name for an app key."""
    return _LABELS.get(app) or (app or "app").replace("_", " ").title()


# ---------------------------------------------------------------------------
# Open WebUI native MCP
# ---------------------------------------------------------------------------
class OwuiMcpProvider:
    name = "owui_mcp"

    def __init__(self, hub_dir, server_id: str, server: dict | None = None):
        self._hub_dir = Path(hub_dir)
        self.server_id = server_id
        self.server = server

    def ref(self) -> str:
        return self.server_id

    def _user_id(self, subject: str) -> str | None:
        from ..access import owui_oauth_tokens as tokens

        return tokens.resolve_user_id(self._hub_dir, subject)

    def status(self, subject: str, app: str) -> str:  # noqa: ARG002
        from .. import owui_refresh
        from ..access import owui_oauth_tokens as tokens

        uid = self._user_id(subject)
        if not uid or not tokens.session_meta(self._hub_dir, uid, self.server_id):
            return "none"
        if owui_refresh.access_token_for(self._hub_dir, uid, self.server_id):
            return "connected"
        return "expired"

    def begin(self, journey: dict) -> str:  # noqa: ARG002
        # Open WebUI's own authorize route. It requires the same signed-in
        # browser session our page just checked.
        return f"/oauth/clients/{quote('mcp:' + self.server_id, safe=':')}/authorize"

    def verify(self, journey: dict) -> str:
        from .. import owui_refresh
        from ..access import owui_oauth_tokens as tokens

        started = journey.get("started")
        if not started:
            return "pending"
        uid = self._user_id(journey["subject"])
        if not uid:
            return "pending"
        meta = tokens.session_meta(self._hub_dir, uid, self.server_id)
        # Open WebUI stores created_at in whole seconds.
        if not meta or meta["created_at"] < int(started):
            return "pending"
        if owui_refresh.access_token_for(self._hub_dir, uid, self.server_id):
            return "connected"
        return "failed"

    def cleanup_duplicates(self, journey: dict) -> None:  # noqa: ARG002
        """Nothing to do: Open WebUI deletes the person's previous session for
        this server before storing the new one (a reconnect replaces it)."""


# ---------------------------------------------------------------------------
# Composio
# ---------------------------------------------------------------------------
class ComposioProvider:
    name = "composio"

    def __init__(self, gate, app: str):
        self._gate = gate
        self.app = app

    def _broker(self):
        broker = self._gate.broker()
        if broker is None:
            raise JourneyError("unavailable", f"{label(self.app)} is not available to connect here.")
        return broker

    @staticmethod
    def _ref(journey: dict) -> dict:
        try:
            ref = json.loads(journey.get("provider_ref") or "{}")
        except ValueError:
            return {}
        return ref if isinstance(ref, dict) else {}

    def prepare(self, *, jid: str, subject: str, callback_url: str) -> str:
        """Create this journey's hosted link now (the origin hub holds the
        Composio key), and keep it server-side in ``provider_ref``."""
        created = self._broker().create_link(user=subject, app=self.app, callback_url=callback_url)
        return json.dumps({"account": created.get("account_id"),
                           "redirect": created["redirect_url"]})

    def status(self, subject: str, app: str) -> str:
        return "connected" if self._broker().is_connected(user=subject, app=app) else "none"

    def begin(self, journey: dict) -> str:
        redirect = self._ref(journey).get("redirect")
        if not redirect:
            raise JourneyError("unavailable", "This link cannot be started. Ask again for a new link.")
        return redirect

    def verify(self, journey: dict) -> str:
        account_id = self._ref(journey).get("account")
        if not account_id or not journey.get("started"):
            return "pending"
        acct = self._broker().account(account_id)
        if not acct:
            return "pending"
        if normalize(acct.get("user_id") or "") != journey["subject"] or (
                acct.get("toolkit") and acct["toolkit"] != journey["app"]):
            return "failed"
        status = acct.get("status")
        if status == "ACTIVE":
            return "connected"
        if status in ("FAILED", "EXPIRED", "REVOKED"):
            return "failed"
        return "pending"

    def cleanup_duplicates(self, journey: dict) -> None:
        """After a verified connection, delete the person's other ACTIVE
        accounts for this app, so a reconnect never leaves two."""
        keep = self._ref(journey).get("account")
        if not keep:
            return
        broker = self._broker()
        for other in broker.active_account_ids(user=journey["subject"], app=journey["app"]):
            if other != keep:
                broker.delete_account(other)
                log.info("connect: removed an older %s connection for %s",
                         journey["app"], journey["subject"])

    def abandon(self, journey: dict) -> None:
        """Invalidate a superseded journey's unused link (best effort)."""
        account_id = self._ref(journey).get("account")
        if not account_id:
            return
        broker = self._broker()
        acct = broker.account(account_id)
        if acct and acct.get("status") not in ("ACTIVE",):
            broker.delete_account(account_id)


# ---------------------------------------------------------------------------
# Resolution: exactly one provider per app per hub
# ---------------------------------------------------------------------------
def _owui_servers_for(hub_dir, app: str) -> list[dict]:
    from .. import owui_mcp
    from ..access import owui_tool_servers as servers

    if not owui_mcp.enabled():
        return []
    return [c for c in servers.list_mcp_connections(hub_dir)
            if c.get("auth_type") in servers.OAUTH_AUTH_TYPES and c.get("enabled", True)
            and owui_mcp.app_key(c["id"]) == app]


def for_app(hub_dir, app: str, *, gate=None) -> "OwuiMcpProvider | ComposioProvider":
    """The one provider that serves ``app`` for this hub.

    Raises JourneyError ``conflict`` when more than one could (naming them, so
    an administrator can remove one), or ``unavailable`` when none can.
    ``gate`` is this hub's Composio gate (default: the process gate, only when
    it belongs to this hub).
    """
    from .. import connections

    hub = Path(hub_dir).name
    candidates: list[tuple[str, object]] = []
    for srv in _owui_servers_for(hub_dir, app):
        candidates.append((f"the Open WebUI tool server '{srv['id']}'",
                           OwuiMcpProvider(hub_dir, srv["id"], srv)))
    gate = gate if gate is not None else connections.gate_for(hub)
    if gate is not None and gate.active and app in gate.allowed:
        candidates.append(("Composio (CONNECTIONS)", ComposioProvider(gate, app)))
    if len(candidates) > 1:
        names = " and ".join(n for n, _ in candidates)
        log.warning("connect: %s has more than one provider on %s: %s", app, hub, names)
        raise JourneyError(
            "conflict",
            f"{label(app)} can be connected through {names}. An administrator must keep "
            "exactly one, so no one ends up with two connections.")
    if not candidates:
        raise JourneyError("unavailable", f"{label(app)} is not available to connect on this hub.")
    return candidates[0][1]


def for_journey(hub_dir, journey: dict, *, gate=None):
    """The provider that can check ``journey`` from this process, or None.

    Open WebUI journeys can be checked by any bridge of the deployment (they
    share Open WebUI's database). A Composio journey needs its own hub's key:
    only that hub's bridge, or its inbound process given ``gate``, can check it.
    """
    from .. import connections

    if journey.get("provider") == OwuiMcpProvider.name:
        return OwuiMcpProvider(hub_dir, journey.get("provider_ref") or "")
    if journey.get("provider") == ComposioProvider.name:
        gate = gate if gate is not None else connections.gate_for(journey.get("hub") or "")
        if gate is None or not gate.active:
            return None
        return ComposioProvider(gate, journey["app"])
    return None


def begin_url(hub_dir, journey: dict) -> str:
    """Where the browser goes to authorize this journey. Needs no provider
    credentials, so any bridge serving the link page can start it: Open WebUI's
    authorize route (the server must still be registered), or the Composio
    link created when the journey was."""
    from ..access import owui_tool_servers as servers

    if journey.get("provider") == OwuiMcpProvider.name:
        server_id = journey.get("provider_ref") or ""
        if not servers.mcp_connection(hub_dir, server_id):
            raise JourneyError("unavailable", f"{label(journey['app'])} is no longer available "
                                              "to connect. Ask your administrator.")
        return OwuiMcpProvider(hub_dir, server_id).begin(journey)
    if journey.get("provider") == ComposioProvider.name:
        return ComposioProvider(None, journey["app"]).begin(journey)
    raise JourneyError("unavailable", "This link cannot be started. Ask again for a new link.")
