"""Seed consented OAuth grants for transport/policy tests (login has its own suite)."""

from __future__ import annotations
import secrets
import time
from hubzoid.mcp_oauth_store import OAuthStore

RESOURCE = "https://hub.example/mcp"


def seed(hub, *, token="oauth-test", email="alice@example.com", account_id="u1"):
    """A consented grant for ``email``'s Hubzoid account (created when missing)."""
    from hubzoid.auth import users

    accounts = users.store(hub)
    if accounts.get(account_id) is None and accounts.find_by_email(email) is None:
        accounts.create(email=email, name=email.split("@")[0], id=account_id, source="admin")
    store = OAuthStore(hub, RESOURCE)
    grant = secrets.token_urlsafe(32)
    with store.engine.begin() as c:
        store.put(
            c,
            grant,
            "grant",
            {
                "id": grant,
                "email": email,
                "account_id": account_id,
                "client_id": "test-client",
                "client_name": "Test client",
            },
            time.time() + 3600,
        )
        store.put(
            c,
            token,
            "access",
            {"grant": grant, "client_id": "test-client", "scopes": ["hub:access"]},
            time.time() + 600,
        )
    return token
