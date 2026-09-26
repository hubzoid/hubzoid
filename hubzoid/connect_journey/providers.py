"""Where a connection is actually made and checked: Open WebUI native MCP.

The provider is an MCP tool server registered in Open WebUI with OAuth 2.1.
Open WebUI runs the authorization and stores the person's token in
``oauth_session``. It answers, always from its own records and never from
browser callback parameters:

  * ``status``: is the person connected now (``connected|none|expired``)?
  * ``begin``: where does the browser go to authorize?
  * ``verify``: did *this* journey connect (``connected|pending|failed``)? A
    session row created after the journey started whose token is usable now.

Each app resolves to exactly one server per hub (:func:`for_app`), so a person
never ends up with two connections for one app. An app with no such server is
not available to connect.
"""
from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import quote

log = logging.getLogger("hubzoid.connect")


class JourneyError(Exception):
    """A journey cannot proceed. ``message`` is safe to show the person."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


_LABELS = {"gmail": "Gmail", "github": "GitHub", "google_calendar": "Google Calendar",
           "google_drive": "Google Drive", "googlecalendar": "Google Calendar",
           "googledrive": "Google Drive", "linear": "Linear", "notion": "Notion",
           "slack": "Slack", "jira": "Jira", "odoo": "Odoo"}


def label(app: str) -> str:
    """A display name for an app key."""
    return _LABELS.get(app) or (app or "app").replace("_", " ").title()


class OwuiMcpProvider:
    name = "owui_mcp"

    def __init__(self, hub_dir, server_id: str):
        self._hub_dir = Path(hub_dir)
        self.server_id = server_id

    def _user_id(self, subject: str) -> str | None:
        from ..access import owui_oauth_tokens as tokens

        return tokens.resolve_user_id(self._hub_dir, subject)

    def status(self, subject: str) -> str:
        from .. import owui_refresh
        from ..access import owui_oauth_tokens as tokens

        uid = self._user_id(subject)
        if not uid or not tokens.session_meta(self._hub_dir, uid, self.server_id):
            return "none"
        if owui_refresh.access_token_for(self._hub_dir, uid, self.server_id):
            return "connected"
        return "expired"

    def begin(self) -> str:
        # Open WebUI's own authorize route. It requires the same signed-in
        # browser session our page just checked.
        return f"/oauth/clients/{quote('mcp:' + self.server_id, safe=':')}/authorize"

    def verify(self, journey: dict) -> str:
        """Open WebUI deletes the person's previous session for this server
        before storing the new one, so a reconnect never leaves two."""
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


# ---------------------------------------------------------------------------
# Resolution: exactly one server per app per hub
# ---------------------------------------------------------------------------
def _owui_servers_for(hub_dir, app: str) -> list[dict]:
    from .. import owui_mcp
    from ..access import owui_tool_servers as servers

    if not owui_mcp.enabled():
        return []
    return [c for c in servers.list_mcp_connections(hub_dir)
            if c.get("auth_type") in servers.OAUTH_AUTH_TYPES and c.get("enabled", True)
            and owui_mcp.app_key(c["id"]) == app]


def for_app(hub_dir, app: str) -> OwuiMcpProvider:
    """The one Open WebUI OAuth MCP server that serves ``app`` for this hub.

    Raises JourneyError ``unavailable`` when there is none, or ``conflict``
    when more than one could (naming them, so an administrator can remove one).
    """
    found = _owui_servers_for(hub_dir, app)
    if not found:
        raise JourneyError("unavailable", f"{label(app)} is not available to connect on this hub.")
    if len(found) > 1:
        names = " and ".join(f"the Open WebUI tool server '{s['id']}'" for s in found)
        log.warning("connect: %s has more than one server on %s: %s",
                    app, Path(hub_dir).name, names)
        raise JourneyError(
            "conflict",
            f"{label(app)} can be connected through {names}. An administrator must keep "
            "exactly one, so no one ends up with two connections.")
    return OwuiMcpProvider(hub_dir, found[0]["id"])


def for_journey(hub_dir, journey: dict) -> OwuiMcpProvider | None:
    """The provider that checks ``journey``, or None for a row it does not
    know. Any bridge of the deployment can check it (they share Open WebUI's
    database)."""
    if journey.get("provider") == OwuiMcpProvider.name:
        return OwuiMcpProvider(hub_dir, journey.get("provider_ref") or "")
    return None


def begin_url(hub_dir, journey: dict) -> str:
    """Where the browser goes to authorize this journey: Open WebUI's
    authorize route, while the server is still registered."""
    from ..access import owui_tool_servers as servers

    if journey.get("provider") != OwuiMcpProvider.name:
        raise JourneyError("unavailable", "This link cannot be started. Ask again for a new link.")
    server_id = journey.get("provider_ref") or ""
    if not servers.mcp_connection(hub_dir, server_id):
        raise JourneyError("unavailable", f"{label(journey['app'])} is no longer available "
                                          "to connect. Ask your administrator.")
    return OwuiMcpProvider(hub_dir, server_id).begin()
