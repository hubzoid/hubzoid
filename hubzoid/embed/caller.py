"""The person an app vouches for, and what they may use here.

No Hubzoid account is created for them. Their chats and runs belong to a
private subject made from the app and the app's user id
(``app_<sha256(app id, user id)>``): it has no ``@`` and no account, so it never
matches a Hubzoid account, an email grant or an "everyone signed in" grant.
The app's user id, name and email are labels only.

Access is the token's request narrowed to the app's maximum, never wider
(``effective``). Tool names are the agent's own tool names,
``connector:<id>`` for a connector, ``mcp:<server>`` for a hub MCP server, or
``*``.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .config import Access, App

SUBJECT_PREFIX = "app_"


def subject(app_id: str, user_id: str) -> str:
    digest = hashlib.sha256(f"{app_id}\0{user_id}".encode("utf-8")).hexdigest()[:40]
    return f"{SUBJECT_PREFIX}{digest}"


def is_app_subject(value: str | None) -> bool:
    return bool(value) and str(value).startswith(SUBJECT_PREFIX) and "@" not in str(value)


def effective(app: App, requested: Access | None) -> Access:
    """What the caller may use: ``requested`` within the app's maximum. None
    (nothing requested) is the maximum. An empty tool list is no tools."""
    maximum = app.maximum
    if requested is None:
        return maximum
    agents = (requested.agents & maximum.agents) if requested.agents else maximum.agents
    if maximum.all_tools():
        tools = requested.tools
    elif requested.all_tools():
        tools = maximum.tools
    else:
        tools = requested.tools & maximum.tools
    return Access(agents=frozenset(agents), tools=frozenset(tools))


def tool_allowed(access: Access, name: str, *, server: str | None = None, connector: str | None = None) -> bool:
    """Whether a tool may run for this caller. ``server`` names a hub MCP
    server and ``connector`` a Settings connector the tool comes from."""
    if access.all_tools():
        return True
    if connector and f"connector:{connector}" in access.tools:
        return True
    if server and f"mcp:{server}" in access.tools:
        return True
    return bool(name) and name in access.tools


@dataclass(frozen=True)
class AppCaller:
    app_id: str
    user_id: str
    subject: str
    name: str
    email: str
    access: Access
    issued_at: float
    expires_at: float
    #: the person's own token for the app's tools; never logged or returned
    app_token: str | None = field(default=None, repr=False)

    def may_use(self, hub: str) -> bool:
        return (hub or "").strip().lower() in self.access.agents

    @property
    def label(self) -> str:
        return self.name or self.email or self.user_id
