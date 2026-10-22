"""Hubzoid chat inside another app (ERPNext, Odoo, an admin dashboard).

An administrator registers the app under ``apps:`` in ``hubzoid.yaml``: its
client id, its web origins, the most it may allow (agents and tools) and how a
person's token is checked. Two modes, both standard JWTs, as Agno and Mastra do:

* ``pass``: the app's server signs a short JWT (a "chat pass") with the client
  secret. It names the person, what they may use and their own app token.
* ``jwt``: the app's own JWT, checked with the app's keys (a JWKS URL, a public
  key or a shared secret). The app needs no server code.

The app's chat talks AG-UI (``/api/agui``), so assistant-ui and CopilotKit work
with their own packages. No Hubzoid account is created: chats belong to a
private subject per app user (``caller.subject``). Tool calls to the app's own
tools carry the person's app token, so the app's own permissions apply.

Modules:

  config    the ``apps:`` section: parse, check, look up by issuer and audience
  tokens    verify a token for one app and return the caller
  caller    the app caller and its effective access (token ∩ the app's maximum)
  scope     the caller's access during a reply (tools, the person's app token)
  agui      the AG-UI endpoint, history, rejoin and approvals
"""

from .scope import app_token  # noqa: E402 - for hub tools that call the app's API

__all__ = ["app_token"]
