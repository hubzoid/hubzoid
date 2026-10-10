"""Where a connection is actually made and checked: a Hubzoid connector
(``hubzoid.connectors``), in both UI modes. Hubzoid runs the authorization
itself and keeps the person's tokens.

The provider answers, always from its own records and never from browser
callback parameters:

  * ``status``: is the person connected now (``connected|none|expired``)?
  * ``begin``: where does the browser go to authorize?
  * ``verify``: did *this* journey connect (``connected|pending|failed``)? A
    connection made after the journey began whose token is usable now.

Connector ids are unique, so an app resolves to exactly one connector and a
person never ends up with two connections for one app. An app with no
switched-on connector is not available to connect. Journeys made through Open
WebUI's own MCP servers before 1.2 have no provider any more and expire.
"""
from __future__ import annotations

import logging
from pathlib import Path

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


class ConnectorProvider:
    """A Hubzoid connector. The authorization runs through
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
        uid = journey.get("account") or self._user_id(journey["subject"])
        if not uid:
            return "pending"
        conn = tokens.get(self._hub_dir, uid, self.server_id)
        if conn is None or conn.connected_at < float(journey.get("created") or 0):
            return "pending"
        return "connected" if self._usable(uid) else "failed"


def _connector_for(hub_dir, app: str):
    from ..connectors import registry

    connector = registry.get(Path(hub_dir), app)
    return connector if connector is not None and connector.enabled else None


def require_available(hub_dir, app: str) -> None:
    """Raise JourneyError ``unavailable`` when no switched-on connector serves
    ``app``."""
    if _connector_for(hub_dir, app) is None:
        raise JourneyError("unavailable", f"{label(app)} is not available to connect on "
                                          "this hub.")


def for_app(hub_dir, app: str):
    """The provider that serves ``app``: its connector. Raises JourneyError
    ``unavailable`` when there is none."""
    require_available(hub_dir, app)
    return ConnectorProvider(hub_dir, app)


def for_journey(hub_dir, journey: dict):
    """The provider that checks ``journey``, or None for a row it does not
    know. Any bridge of the deployment can check it (they share the
    operational store)."""
    if journey.get("provider") == ConnectorProvider.name:
        return ConnectorProvider(hub_dir, journey.get("provider_ref") or "")
    return None


def begin_url(hub_dir, journey: dict, *, request=None, user=None) -> str:
    """Where the browser goes to authorize this journey: the connector's
    authorization, which needs the page's ``request`` and the signed-in ``user``."""
    if journey.get("provider") != ConnectorProvider.name or request is None or user is None:
        raise JourneyError("unavailable", "This link cannot be started. Ask again for a new link.")
    return ConnectorProvider(hub_dir, journey.get("provider_ref") or "").begin(
        journey, request=request, user=user)
