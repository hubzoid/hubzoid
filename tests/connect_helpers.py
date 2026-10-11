"""Shared fakes for the connection-journey and personal-MCP tests.

Since 1.2 every UI mode uses the connectors added in the Console, so these
helpers keep their Open WebUI-shaped arguments (accounts with Open WebUI ids,
servers, connections) and store them as Hubzoid connectors, identities and
encrypted tokens in the test's operational store:

* ``seed_owui`` / ``connect`` record accounts, servers and connections.
* ``owui_env`` selects Open WebUI mode with a deployment key.
* ``isolated_store`` points every operational-DB user at one SQLite file and
  writes what was recorded. Every seeded connector is offered in every agent,
  as a server registered once used to be.
* An in-process FastMCP HTTP server that answers only the bearers it knows.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import socket
import threading
import time
from pathlib import Path

_SEEDS: dict[str, dict] = {}
_ACTIVE: dict = {}


def fernet_key(secret: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()).decode()


def seed_owui(db, *, users, servers, secret):  # noqa: ARG001 — the secret is owui_env's
    """``users``: [(account id, email)]. ``servers``: [{id, name, url, auth_type?,
    allow?, enable?}]; a non-OAuth (``bearer``) server is not a connector."""
    _SEEDS[str(db)] = {"users": list(users), "servers": list(servers), "tokens": [],
                       "applied": None}


def connect(db, *, user_id, server_id, secret=None, access_token,  # noqa: ARG001
            created_at=None, expires_in=3600):
    """A person's connection: replaces any earlier one for the server."""
    now = time.time() if created_at is None else float(created_at)
    token = {"access_token": access_token, "refresh_token": "RT-" + access_token,
             "expires_at": int(now) + expires_in, "token_type": "Bearer"}
    seed = _SEEDS[str(db)]
    seed["tokens"].append((user_id, server_id, token, now))
    if seed["applied"] is not None:
        _store_token(seed, user_id, server_id, token, now)


def _email(seed, user_id):
    return next(e for uid, e in seed["users"] if uid == user_id)


def _store_token(seed, user_id, server_id, token, now):
    from hubzoid.connectors import tokens

    tokens.store(seed["applied"], user_id=user_id, email=_email(seed, user_id),
                 connector_id=server_id, token=dict(token), now=now)


def owui_env(monkeypatch, db, secret):
    monkeypatch.setenv("HUBZOID_SECRET_KEY", fernet_key(secret))
    monkeypatch.delenv("OWUI_NATIVE_MCP", raising=False)
    monkeypatch.setenv("HUBZOID_UI", "openwebui")
    from hubzoid import secretbox

    getattr(secretbox, "_cache", {}).clear()
    _ACTIVE["db"] = str(db)


def isolated_store(tmp_path, monkeypatch):
    """Point every operational-DB user at one SQLite file for this test, and
    write what ``seed_owui`` and ``connect`` recorded."""
    import hubzoid.access as access
    import hubzoid.db as db
    from sqlalchemy import create_engine

    eng = create_engine(f"sqlite:///{tmp_path / 'ops.db'}",
                        connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "operational_engine", lambda *a, **k: eng)
    monkeypatch.setattr(db, "engine_for", lambda *a, **k: eng)
    access._stores.clear()
    seed = _SEEDS.get(_ACTIVE.pop("db", ""))
    if seed is not None:
        _apply(seed, Path(tmp_path), monkeypatch)
    return eng


def _apply(seed, root: Path, monkeypatch) -> None:
    import hubzoid.access as access
    from hubzoid.connectors import ConnectorError, registry

    seed["applied"] = root
    gs = access.store_for(root)
    for uid, email in seed["users"]:
        gs.upsert_identity(email=email, owui_id=uid)
    ids: set[str] = set()
    for srv in seed["servers"]:
        if srv.get("auth_type", "oauth_2.1") not in ("oauth_2.1", "oauth", "oauth_2.1_static"):
            continue
        body = {"id": srv["id"], "name": srv.get("name", srv["id"]), "url": srv["url"],
                "auth_type": "oauth", "enabled": bool(srv.get("enable", True))}
        if srv.get("allow"):
            body["tool_allowlist"] = srv["allow"]
        try:
            registry.create(root, body, actor="test")
        except ConnectorError as err:
            if err.code != "exists":
                raise
        ids.add(srv["id"])
    real = registry.offered_in

    def offered_in(hub_dir, hub, *, conn=None):
        return set(real(hub_dir, hub, conn=conn)) | ids

    monkeypatch.setattr(registry, "offered_in", offered_in)
    import hubzoid.connectors as connectors

    monkeypatch.setattr(connectors, "existing_engine", lambda hub_dir: connectors.engine(hub_dir))
    for user_id, server_id, token, now in seed["tokens"]:
        _store_token(seed, user_id, server_id, token, now)


def grant(hub, *emails, permissions=("connector_gmail", "connector_wiki")) -> None:
    """Give each person the connector capabilities in this hub (Console grants)."""
    import hubzoid.access as access

    gs = access.store_for(hub)
    for email in emails:
        for permission in permissions:
            gs.grant(email, hub.name, permission, actor="test")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def mcp_server(name: str, bearers: dict[str, str], tools: dict):
    """Serve a FastMCP Streamable HTTP server on a loopback port.

    ``bearers`` maps an accepted bearer token to the person it belongs to. Any
    other (or no) bearer gets 401. ``tools`` maps a tool name to a function
    ``fn(person) -> str``. Yields the server URL.
    """
    import uvicorn
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
        # Answer only known bearers (lifespan events pass straight through).
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            auth = headers.get(b"authorization", b"").decode()
            if auth.removeprefix("Bearer ").strip() not in bearers:
                await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
                return
        await inner(scope, receive, send)

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="warning", lifespan="on"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("test MCP server did not start")
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
