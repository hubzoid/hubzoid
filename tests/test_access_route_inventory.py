"""Every API route checks who is asking: on a real bridge with sign-in on, a
signed-out request to any ``/api``, ``/portal/api`` or ``/v1`` route is refused,
except the few routes that are public on purpose (listed here, with why).

A new route that forgets its check fails this test, so a hidden button can
never be the only thing standing in the way."""
from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from hubzoid.auth import sessions

from tests.test_auth_routes import ENV

# Public on purpose: signing in, the sign-in page's brand, link pages, share
# pages and the hosted-MCP OAuth endpoints (each checks its own credential).
PUBLIC = (
    r"^/api/auth/(session|login|logout|signup|link/.*|mfa/verify|mfa/setup)$",
    # Company sign-in: the identity provider posts back here signed, and the
    # first-party ticket page finishes it (each checks its own proof).
    r"^/api/auth/saml/[^/]+/(login|acs|done)$",
    r"^/oauth/.*", r"^/api/branding.*", r"^/branding/.*", r"^/api/shares/public/.*",
    r"^/api/s/.*", r"^/healthz$", r"^/api/parity/schema$", r"^/api/chat/config/schema$",
    r"^/\.well-known/.*", r"^/mcp.*", r"^/api/connections/callback.*",
    r"^/portal/api/connectors/callback.*", r"^/api/version$",
    # What the page needs before anyone signs in (no grants, names or private
    # settings): theme, defaults, device support, a connection ping, and the
    # empty sandboxed frame artifacts render into.
    r"^/api/chat/(bootstrap|theme|connection|devices/support|devices/settings|artifact-frame)$",
)
REFUSED = {401, 403, 404, 405, 409, 410, 422, 503}


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    for key in ENV:
        monkeypatch.delenv(key, raising=False)
    hub = tmp_path / "minimal"
    shutil.copytree(Path(__file__).parent / "fixtures" / "minimal_hub", hub)
    monkeypatch.setenv("HUBZOID_HUB_DIR", str(hub))
    monkeypatch.setenv("MODEL", "openrouter/anthropic/claude-haiku-4.5")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-used")
    monkeypatch.setenv("BRIDGE_API_KEYS", "dev")
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    monkeypatch.setenv("HUBZOID_AUTH", "true")
    sessions.reset_cache()
    from hubzoid.server import build_app

    return build_app()


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}:]+(:path)?\}", "x1", path)


def test_every_route_refuses_a_signed_out_caller(bridge):
    c = TestClient(bridge, raise_server_exceptions=False)
    unguarded = []
    for route in bridge.routes:
        if not isinstance(route, APIRoute):
            continue
        path = route.path
        if not path.startswith(("/api", "/portal/api", "/v1", "/oauth")):
            continue
        if any(re.match(p, path) for p in PUBLIC):
            continue
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            r = c.request(method, _concrete(path), headers={"origin": "http://testserver"},
                          json={} if method in ("POST", "PUT", "PATCH") else None)
            if r.status_code not in REFUSED:
                unguarded.append(f"{method} {path} -> {r.status_code}")
    assert not unguarded, "routes answering a signed-out caller:\n" + "\n".join(unguarded)
