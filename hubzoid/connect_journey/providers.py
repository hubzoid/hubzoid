"""Where a connection is actually made and checked.

Two providers, one per UI mode:

  * Default mode: a Hubzoid connector (``hubzoid.connectors``). Hubzoid runs the
    authorization itself and keeps the person's tokens.
  * Open WebUI mode (``HUBZOID_UI=openwebui``): an MCP tool server registered in
    Open WebUI with OAuth 2.1. Open WebUI runs the authorization and stores the
    person's token in ``oauth_session``.

Each answers, always from its own records and never from browser callback
parameters:

  * ``status``: is the person connected now (``connected|none|expired``)?
  * ``begin``: where does the browser go to authorize?
  * ``verify``: did *this* journey connect (``connected|pending|failed``)? A
    connection made after the journey began whose token is usable now.

Each app resolves to exactly one server per hub (:func:`for_app`), so a person
never ends up with two connections for one app. An app with no such server is
not available to connect. A journey keeps the provider it was created with.
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


class ConnectorProvider:
    """A Hubzoid connector (default UI mode). The authorization runs through
    ``hubzoid.connectors.oauth_flow`` and returns to the journey's done page;
    the answer comes from the stored connection, never from the callback."""

    name = "hz_connector"

    def __init__(self, hub_dir, connector_id: str):
        self._hub_dir = Path(hub_dir)
        self.server_id = connector_id

    def _user_id(self, subject: str) -> str | None:
        from ..connectors import tokens

        return tokens.user_id_for(self._hub_dir, subject)

    def _usable(self, user_id: str) -> bool:
        from ..connectors import registry, tokens

        connector = registry.get(self._hub_dir, self.server_id)
        if connector is None:
            return False
        if connector.auth_type == "none":
            return tokens.get(self._hub_dir, user_id, self.server_id) is not None
        return bool(tokens.access_token_for(self._hub_dir, user_id, self.server_id,
                                            url=connector.url))

    def status(self, subject: str) -> str:
        from ..connectors import tokens

        uid = self._user_id(subject)
        if not uid or tokens.get(self._hub_dir, uid, self.server_id) is None:
            return "none"
        return "connected" if self._usable(uid) else "expired"

    def begin(self, journey: dict, *, request, user) -> str:
        """Start the authorization for the journey's signed-in subject and
        return where the browser goes: the provider, or straight to the done
        page for a connector that needs no sign-in."""
        from ..connectors import ConnectorError, oauth_flow, registry

        connector = registry.get(self._hub_dir, self.server_id)
        if connector is None or not connector.enabled:
            raise JourneyError("unavailable", f"{label(journey['app'])} is no longer available "
                                              "to connect. Ask your administrator.")
        done = f"/portal/connect/{journey['id']}/done"
        try:
            if connector.auth_type == "none":
                oauth_flow.connect_without_auth(self._hub_dir, connector, user)
                return done
            return oauth_flow.start(self._hub_dir, connector, user,
                                    origin=oauth_flow.origin_for(request), return_to=done,
                                    journey_id=journey["id"])
        except ConnectorError as err:
            log.warning("connect: %s could not start (%s)", self.server_id, err.code)
            raise JourneyError("unavailable", err.message) from None

    def verify(self, journey: dict) -> str:
        """A connection made after the link was created, usable now. A refresh
        never moves ``connected_at``; only a new authorization does."""
        from ..connectors import tokens

        if not journey.get("started"):
            return "pending"
        uid = self._user_id(journey["subject"])
        if not uid:
            return "pending"
        conn = tokens.get(self._hub_dir, uid, self.server_id)
        if conn is None or conn.connected_at < float(journey.get("created") or 0):
            return "pending"
        return "connected" if self._usable(uid) else "failed"


def _legacy(hub_dir) -> bool:
    from .. import appmode

    return appmode.is_openwebui(Path(hub_dir))


def _connector_for(hub_dir, app: str):
    from ..connectors import registry

    connector = registry.get(Path(hub_dir), app)
    return connector if connector is not None and connector.enabled else None


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


def require_available(hub_dir, app: str) -> None:
    """Raise JourneyError ``unavailable`` when nothing serves ``app`` on this
    hub: no switched-on connector (default mode), no Open WebUI OAuth MCP
    server (Open WebUI mode)."""
    if not _legacy(hub_dir):
        if _connector_for(hub_dir, app) is None:
            raise JourneyError("unavailable", f"{label(app)} is not available to connect on "
                                              "this hub.")
        return
    if not _owui_servers_for(hub_dir, app):
        raise JourneyError("unavailable", f"{label(app)} is not available to connect on this hub.")


def for_app(hub_dir, app: str):
    """The provider that serves ``app`` for this hub: its connector (default
    mode; connector ids are unique, so never a conflict), or the one Open WebUI
    OAuth MCP server (Open WebUI mode).

    Raises JourneyError ``unavailable`` when there is none, or ``conflict``
    when more than one could. The server ids go to the log for the
    administrator, never to the person asking.
    """
    if not _legacy(hub_dir):
        connector = _connector_for(hub_dir, app)
        if connector is None:
            raise JourneyError("unavailable", f"{label(app)} is not available to connect on "
                                              "this hub.")
        return ConnectorProvider(hub_dir, connector.id)
    found = _owui_servers_for(hub_dir, app)
    if not found:
        raise JourneyError("unavailable", f"{label(app)} is not available to connect on this hub.")
    if len(found) > 1:
        log.warning("connect: %s has more than one Open WebUI tool server on %s: %s",
                    app, Path(hub_dir).name, ", ".join(s["id"] for s in found))
        raise JourneyError(
            "conflict",
            f"{label(app)} is set up more than once on this hub. Ask an administrator to "
            "keep exactly one, so no one ends up with two connections.")
    return OwuiMcpProvider(hub_dir, found[0]["id"])


def for_journey(hub_dir, journey: dict):
    """The provider that checks ``journey``, or None for a row it does not
    know. Any bridge of the deployment can check it (they share the
    operational store, and in Open WebUI mode Open WebUI's database)."""
    if journey.get("provider") == OwuiMcpProvider.name:
        return OwuiMcpProvider(hub_dir, journey.get("provider_ref") or "")
    if journey.get("provider") == ConnectorProvider.name:
        return ConnectorProvider(hub_dir, journey.get("provider_ref") or "")
    return None


def begin_url(hub_dir, journey: dict, *, request=None, user=None) -> str:
    """Where the browser goes to authorize this journey: the connector's
    authorization (default mode, which needs the page's ``request`` and the
    signed-in ``user``), or Open WebUI's authorize route while the server is
    still registered (Open WebUI mode)."""
    from ..access import owui_tool_servers as servers

    if journey.get("provider") == ConnectorProvider.name:
        if request is None or user is None:
            raise JourneyError("unavailable", "This link cannot be started. Ask again for a "
                                              "new link.")
        return ConnectorProvider(hub_dir, journey.get("provider_ref") or "").begin(
            journey, request=request, user=user)
    if journey.get("provider") != OwuiMcpProvider.name:
        raise JourneyError("unavailable", "This link cannot be started. Ask again for a new link.")
    server_id = journey.get("provider_ref") or ""
    if not servers.mcp_connection(hub_dir, server_id):
        raise JourneyError("unavailable", f"{label(journey['app'])} is no longer available "
                                          "to connect. Ask your administrator.")
    return OwuiMcpProvider(hub_dir, server_id).begin()
