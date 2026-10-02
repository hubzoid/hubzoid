"""Shared fakes for the personal connection tests (``tests/test_connectors_*``).

* ``clean_env``: the default UI mode, sign-in off, nothing configured.
* ``OAuthMcp``: a REAL OAuth-protected MCP server, Hubzoid's own hosted MCP
  surface (``mcp_server.build_mcp_app`` with ``mcp_oauth.HubOAuth``) on a
  loopback port in 3500-3599. Only its external login (the Open WebUI session
  check behind its consent page) is replaced, exactly as tests/test_mcp_oauth.py
  does.
* ``Browser``: follows an authorize URL through that server's consent page with
  its CSRF form, the way a browser would, and returns the callback URL.
* ``bearer_mcp``: a small MCP server that answers only the bearers it knows.
* ``app_for``: a FastAPI app with the connection routes (and journey pages).
* ``accounts``: sign-in on, with test accounts chosen by an ``x-test-user`` header.
"""
from __future__ import annotations

import contextlib
import re
import socket
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

PORTS = range(3540, 3600)
OWNER = "admin@localhost"

_ENV = ("HUBZOID_UI", "HUBZOID_AUTH", "WEBUI_AUTH", "HUBZOID_PUBLIC_URL", "WEBUI_URL",
        "HUBZOID_ALLOWED_ORIGINS", "HUBZOID_SECRET_KEY", "DATABASE_URL", "HUBZOID_OPERATIONAL_DB",
        "HUBZOID_DEPLOYMENT", "HUBZOID_RESTRICTED_SURFACES", "OWUI_NATIVE_MCP",
        "HUBZOID_CONNECT_JOURNEY", "HUBZOID_PORTAL_DEV", "HUBZOID_PORTAL_DEV_USER")


def clean_env(monkeypatch) -> None:
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    import hubzoid.access as access
    from hubzoid.connectors import oauth_flow

    access._stores.clear()
    oauth_flow.forget_discovery()


def make_hub(root: Path, name: str) -> Path:
    hub = Path(root) / name
    hub.mkdir(parents=True, exist_ok=True)
    (hub / "AGENTS.md").write_text(f"---\nname: {name}\n---\n\nYou are {name}.\n")
    return hub


def grant_connector(hub: Path, connector_id: str, *emails: str) -> None:
    """Give people a connector's capability in this hub (Console grants).
    Default: the local owner."""
    from hubzoid.access import store_for

    for email in emails or (OWNER,):
        store_for(hub).grant(email, Path(hub).name, "connector_" + connector_id, actor="test")


def free_port(taken: set[int] | None = None) -> int:
    for port in PORTS:
        if taken and port in taken:
            continue
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("no free port in 3540-3599")


def serve(app, port: int):
    """Run an ASGI app on 127.0.0.1:``port`` in a thread. Returns a stop()."""
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                           lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("test server did not start")

    def stop():
        server.should_exit = True
        thread.join(timeout=10)

    return stop


# ---------------------------------------------------------------------------
# The real OAuth-protected MCP server
# ---------------------------------------------------------------------------
class OAuthMcp:
    """Hubzoid's hosted MCP surface for a scratch hub, OAuth required."""

    def __init__(self, root: Path, *, email: str = OWNER):
        self.root = Path(root)
        self.email = email
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        self.origin = f"http://127.0.0.1:{self.port}"
        self._mp = None
        self._stop = None

    def __enter__(self):
        import pytest

        from hubzoid import mcp_server
        from hubzoid import settings as settingslib
        from hubzoid.access import session

        hub = make_hub(self.root, "mcphub")
        (hub / "knowledge").mkdir(exist_ok=True)
        (hub / "knowledge" / "widgets.md").write_text("# Widgets\nThe widget count is 42.\n")
        owui = self.root / "mcp-owui.db"
        con = sqlite3.connect(owui)
        con.execute('CREATE TABLE "user"(id TEXT PRIMARY KEY, email TEXT)')
        con.execute('INSERT INTO "user" VALUES ("u1", ?)', (self.email,))
        con.commit()
        con.close()
        self.hub = hub
        # The consent page resolves the signed-in person to a Hubzoid account in
        # the default UI mode (auth.users.mcp_account); the Open WebUI user table
        # above serves the legacy mode.
        from hubzoid.auth import users

        if users.find_by_email(hub, self.email) is None:
            users.create(hub, email=self.email, name="MCP server owner", role="admin")
        from hubzoid.access import store_for

        store_for(hub).grant(self.email, hub.name, "use_hub", actor="test")
        self._mp = pytest.MonkeyPatch()
        self._mp.setenv("HUBZOID_OWUI_DB", str(owui))
        self._mp.setenv("MCP_SERVER", "true")
        self._mp.setenv("MCP_PUBLIC_URL", self.url)
        # The consent page's login check: the browser below carries this cookie.
        self._mp.setattr(session, "verified_email", lambda req, hub_dir=None, **kw: (
            self.email if req.cookies.get("token") == "owui-session" else ""))
        app = mcp_server.build_mcp_app(hub, settings=settingslib.load(hub))
        self._stop = serve(app, self.port)
        return self

    def __exit__(self, *exc):
        if self._stop:
            self._stop()
        if self._mp:
            self._mp.undo()

    def register_client(self, redirect_uri: str) -> str:
        """Register a client by hand, as an administrator would at a provider."""
        r = httpx.post(f"{self.url}/oauth/register", json={
            "client_name": "Hubzoid (manual)", "redirect_uris": [redirect_uri],
            "token_endpoint_auth_method": "none", "scope": "hub:access",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})
        assert r.status_code == 201, r.text
        return r.json()["client_id"]


class Browser:
    """A person's browser at the MCP server: signed in there, consenting."""

    def __init__(self, server: OAuthMcp):
        self.server = server
        self.http = httpx.Client(cookies={"token": "owui-session"}, follow_redirects=False,
                                 timeout=15.0)

    def close(self):
        self.http.close()

    def consent(self, authorize_url: str, decision: str = "allow") -> str:
        """Follow ``authorize_url`` to the consent page, submit the form and
        return the callback (path and query) the server redirects to."""
        r = self.http.get(authorize_url)
        assert r.status_code == 302, r.text
        consent = r.headers["location"]
        if consent.startswith("/"):
            consent = self.server.origin + consent
        r = self.http.get(consent)
        assert r.status_code == 200, r.text
        csrf = re.search(r'name="csrf" value="([^"]+)"', r.text).group(1)
        r = self.http.post(consent, data={"csrf": csrf, "decision": decision},
                           headers={"Origin": self.server.origin})
        assert r.status_code == 303, r.text
        back = urlsplit(r.headers["location"])
        return back.path + "?" + back.query


# ---------------------------------------------------------------------------
# A plain MCP server that knows its bearers
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def bearer_mcp(name: str, bearers: dict[str, str], tools: dict):
    """``bearers`` maps an accepted bearer to its person; ``tools`` maps a tool
    name to ``fn(person) -> str``. Anything else gets 401. Yields the URL."""
    from fastmcp import FastMCP
    from fastmcp.server.dependencies import get_http_headers
    from starlette.responses import JSONResponse

    mcp = FastMCP(name)

    def person() -> str:
        auth = get_http_headers(include={"authorization"}).get("authorization", "")
        return bearers.get(auth.removeprefix("Bearer ").strip(), "")

    for tool_name, fn in tools.items():
        def make(fn=fn):
            def tool() -> str:
                return fn(person())
            return tool
        mcp.tool(name=tool_name)(make())
    inner = mcp.http_app(path="/mcp")

    async def app(scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            auth = headers.get(b"authorization", b"").decode()
            if bearers and auth.removeprefix("Bearer ").strip() not in bearers:
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await inner(scope, receive, send)

    port = free_port()
    stop = serve(app, port)
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        stop()


# ---------------------------------------------------------------------------
# The app under test
# ---------------------------------------------------------------------------
def app_for(hub: Path, *, journeys: bool = False):
    from fastapi import FastAPI

    from hubzoid.connectors import routes

    app = FastAPI()
    routes.mount(app, hub)
    if journeys:
        from hubzoid import connect_journey

        app.include_router(connect_journey.build_router(hub))
    return app


def client_for(app, origin: str):
    from fastapi.testclient import TestClient

    return TestClient(app, base_url=origin, follow_redirects=False)


# ---------------------------------------------------------------------------
# Accounts (sign-in on) without the sign-in lane: a header picks the account
# ---------------------------------------------------------------------------
def add_user(hub: Path, user_id: str, email: str, *, role: str = "user",
             status: str = "active") -> None:
    from sqlalchemy import text

    from hubzoid import connectors

    now = time.time()
    with connectors.engine(hub).begin() as conn:
        conn.execute(text("INSERT INTO hz_users (id, email, role, status, created_at, updated_at) "
                          "VALUES (:i, :e, :r, :s, :n, :n)"),
                     {"i": user_id, "e": email, "r": role, "s": status, "n": now})


def accounts(monkeypatch, hub: Path, people: dict[str, tuple[str, str]]) -> None:
    """Sign-in on. ``people`` maps an ``x-test-user`` header value to
    ``(user id, role)``; the account's email is the header value."""
    from hubzoid.auth import AuthUser, sessions

    monkeypatch.setenv("HUBZOID_AUTH", "true")
    for email, (uid, role) in people.items():
        add_user(hub, uid, email, role=role)

    def resolve(request, hub_dir):  # noqa: ARG001
        email = request.headers.get("x-test-user", "")
        if email in people:
            uid, role = people[email]
            return AuthUser(id=uid, email=email, name=email, role=role, method="password")
        return None

    monkeypatch.setattr(sessions, "resolve", resolve)
