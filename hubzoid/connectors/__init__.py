"""Personal connections: remote MCP servers people connect with their own account.

Default UI mode only (``HUBZOID_UI`` unset or ``hubzoid``). Hubzoid owns the whole
journey: an administrator registers a server once, each person authorizes it
with their own account in the browser, and every chat turn reaches the server
as that person. The legacy Open WebUI mode keeps Open WebUI's MCP connections
(``hubzoid.owui_mcp``) unchanged.

  registry.py    servers an administrator registers (``hz_connectors``)
  discovery.py   where a server's authorization lives (RFC 9728, RFC 8414)
  oauth_flow.py  the browser authorization in two halves: start, callback
  tokens.py      each person's tokens: storage, refresh, revocation
  per_user.py    the servers a caller may reach this turn (runtimes read it)
  routes.py      HTTP: Console registry, people's connections, OAuth callback

Every secret at rest (tokens, client secrets, registered clients, PKCE
verifiers) is encrypted with the deployment key (``hubzoid.secretbox``). None of
them is ever logged or returned by the API.
"""
from __future__ import annotations

from pathlib import Path

CAPABILITY_PREFIX = "connector_"


class ConnectorError(Exception):
    """A refused or failed connector operation.

    ``code`` is stable snake_case for programs, ``message`` a sentence safe to
    show people (never a secret or a raw provider response), ``status`` the
    HTTP status a route answers with.
    """

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def capability(connector_id: str) -> str:
    """The capability that gates a connector on a managed hub: ``connector_<id>``."""
    return f"{CAPABILITY_PREFIX}{connector_id}"


def engine(hub_dir):
    """The shared operational store, migrated. Every bridge of a deployment
    reads the same one, so any bridge serves any connection."""
    from .. import db, migrations

    eng = db.operational_engine(Path(hub_dir))
    migrations.upgrade(eng, "operational")
    return eng
