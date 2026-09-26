"""Shared fakes for the connection-journey and personal-MCP tests.

* An Open WebUI-shaped SQLite database (users, Fernet-encrypted
  ``oauth_session`` rows, registered MCP tool servers), written exactly the
  way Open WebUI 0.11 writes it.
* An in-process FastMCP HTTP server that answers only the bearers it knows.
* An isolated operational store per test.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import socket
import sqlite3
import threading
import time
import uuid


def fernet(secret: str):
    from cryptography.fernet import Fernet

    key = (base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
           if len(secret) != 44 else secret.encode())
    return Fernet(key)


def seed_owui(db, *, users, servers, secret):
    """``users``: [(id, email)]. ``servers``: [{id, name, url, auth_type?, allow?, enable?}]."""
    con = sqlite3.connect(db)
    con.execute('CREATE TABLE IF NOT EXISTS "user" (id TEXT PRIMARY KEY, email TEXT)')
    con.execute("CREATE TABLE IF NOT EXISTS oauth_session (id TEXT PRIMARY KEY, user_id TEXT, "
                "provider TEXT, token TEXT, expires_at BIGINT, created_at BIGINT, updated_at BIGINT)")
    con.execute("CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT, updated_at BIGINT)")
    for uid, email in users:
        con.execute('INSERT INTO "user" VALUES (?,?)', (uid, email))
    conns = []
    for s in servers:
        cfg = {}
        if s.get("allow"):
            cfg["function_name_filter_list"] = s["allow"]
        if "enable" in s:
            cfg["enable"] = s["enable"]
        conns.append({"type": "mcp", "url": s["url"], "auth_type": s.get("auth_type", "oauth_2.1"),
                      "info": {"id": s["id"], "name": s.get("name", s["id"])}, "config": cfg})
    con.execute("INSERT OR REPLACE INTO config VALUES (?,?,?)",
                ("tool_server.connections", json.dumps(conns), int(time.time())))
    con.commit()
    con.close()


def connect(db, *, user_id, server_id, secret, access_token, created_at=None, expires_in=3600):
    """What Open WebUI's MCP OAuth callback does: delete the user's previous
    session for the server, then insert a new one."""
    now = int(time.time()) if created_at is None else int(created_at)
    token = {"access_token": access_token, "refresh_token": "RT-" + access_token,
             "expires_at": now + expires_in}
    enc = fernet(secret).encrypt(json.dumps(token).encode()).decode()
    con = sqlite3.connect(db)
    con.execute("DELETE FROM oauth_session WHERE user_id = ? AND provider = ?",
                (user_id, f"mcp:{server_id}"))
    con.execute("INSERT INTO oauth_session VALUES (?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), user_id, f"mcp:{server_id}", enc, now + expires_in, now, now))
    con.commit()
    con.close()


def owui_env(monkeypatch, db, secret):
    from hubzoid.access import owui_oauth_tokens as tok

    monkeypatch.setenv("HUBZOID_OWUI_DB", str(db))
    monkeypatch.setenv("WEBUI_SECRET_KEY", secret)
    monkeypatch.setenv("OWUI_NATIVE_MCP", "true")
    tok._fernet_cache.clear()


def isolated_store(tmp_path, monkeypatch):
    """Point every operational-DB user at one SQLite file for this test."""
    import hubzoid.access as access
    import hubzoid.db as db
    from sqlalchemy import create_engine

    eng = create_engine(f"sqlite:///{tmp_path / 'ops.db'}",
                        connect_args={"check_same_thread": False})
    monkeypatch.setattr(db, "operational_engine", lambda *a, **k: eng)
    monkeypatch.setattr(db, "engine_for", lambda *a, **k: eng)
    access._stores.clear()
    return eng


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
