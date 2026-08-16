"""per_user_specs ties OWUI's oauth_session + tool_server.connections into
Claude-SDK http MCP specs carrying the caller's own Bearer.

Seeds a full OWUI-shaped webui.db (user, encrypted MCP token, a registered
MCP tool server) and asserts the specs, the allow-list, and every fail-closed
path.
"""
from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import time

import pytest

from hubzoid import owui_mcp
from hubzoid.access import owui_tool_servers as srv
from hubzoid.access import owui_oauth_tokens as tok
from hubzoid.access.identity import Identity


def _fernet(secret: str):
    from cryptography.fernet import Fernet

    key = (base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
           if len(secret) != 44 else secret.encode())
    return Fernet(key)


def _seed(path, *, user_id, email, server_id, url, token, secret, allow=None):
    con = sqlite3.connect(path)
    con.execute('CREATE TABLE "user" (id TEXT PRIMARY KEY, email TEXT)')
    con.execute("CREATE TABLE oauth_session (id TEXT PRIMARY KEY, user_id TEXT, "
                "provider TEXT, token TEXT, expires_at BIGINT, created_at BIGINT, updated_at BIGINT)")
    con.execute("CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT, updated_at BIGINT)")
    con.execute('INSERT INTO "user" VALUES (?,?)', (user_id, email))
    now = int(time.time())
    enc = _fernet(secret).encrypt(json.dumps(token).encode()).decode()
    con.execute("INSERT INTO oauth_session VALUES (?,?,?,?,?,?,?)",
                ("s1", user_id, tok.mcp_provider(server_id), enc, now + 3600, now, now))
    conn = {"type": "mcp", "url": url, "auth_type": "oauth_2.1",
            "info": {"id": server_id, "name": "Odoo ERP"},
            "config": ({"function_name_filter_list": allow} if allow else {})}
    con.execute("INSERT INTO config VALUES (?,?,?)",
                ("tool_server.connections", json.dumps([conn]), now))
    con.commit()
    con.close()


@pytest.fixture
def owui(tmp_path, monkeypatch):
    secret = "shared-webui-secret"
    db = tmp_path / "webui.db"
    facts = dict(user_id="u1", email="bob@x.org", server_id="srv1",
                 url="https://mcp.example.com/mcp", secret=secret, db=db,
                 token={"access_token": "AT-live", "refresh_token": "RT",
                        "expires_at": int(time.time()) + 3600})
    _seed(db, user_id=facts["user_id"], email=facts["email"], server_id=facts["server_id"],
          url=facts["url"], token=facts["token"], secret=secret)
    monkeypatch.setenv("HUBZOID_OWUI_DB", str(db))
    monkeypatch.setenv("WEBUI_SECRET_KEY", secret)
    monkeypatch.delenv("OWUI_NATIVE_MCP", raising=False)
    tok._fernet_cache.clear()
    return facts


def _ident(email):
    return Identity.make(user=email, groups=[], surface="owui")


def test_list_mcp_connections(owui):
    conns = srv.list_mcp_connections(".")
    assert len(conns) == 1
    assert conns[0]["id"] == "srv1"
    assert conns[0]["url"] == owui["url"]


def test_per_user_specs_injects_bearer(owui):
    specs, allowed = owui_mcp.per_user_specs(".", _ident(owui["email"]))
    assert len(specs) == 1
    (key, spec), = specs.items()
    assert key.startswith("owui_")
    assert spec["type"] == "http"
    assert spec["url"] == owui["url"]
    assert spec["headers"]["Authorization"] == "Bearer AT-live"
    assert allowed == [f"mcp__{key}__*"]


def test_allow_list_is_honored(tmp_path, monkeypatch):
    secret = "s"
    db = tmp_path / "webui.db"
    _seed(db, user_id="u1", email="c@x.org", server_id="srv1",
          url="https://m/mcp", token={"access_token": "AT"}, secret=secret,
          allow=["get_partner", "list_invoices"])
    monkeypatch.setenv("HUBZOID_OWUI_DB", str(db))
    monkeypatch.setenv("WEBUI_SECRET_KEY", secret)
    tok._fernet_cache.clear()
    specs, allowed = owui_mcp.per_user_specs(".", _ident("c@x.org"))
    (key, _), = specs.items()
    assert set(allowed) == {f"mcp__{key}__get_partner", f"mcp__{key}__list_invoices"}


def test_anonymous_and_killswitch_and_unknown(owui, monkeypatch):
    # Anonymous caller -> nothing.
    assert owui_mcp.per_user_specs(".", Identity.make(user=None)) == ({}, [])
    # Kill-switch off.
    monkeypatch.setenv("OWUI_NATIVE_MCP", "0")
    assert owui_mcp.per_user_specs(".", _ident(owui["email"])) == ({}, [])
    monkeypatch.setenv("OWUI_NATIVE_MCP", "1")
    # Unknown user -> nothing.
    assert owui_mcp.per_user_specs(".", _ident("ghost@x.org")) == ({}, [])


def test_connected_but_server_unregistered_is_skipped(tmp_path, monkeypatch):
    # A user with an oauth_session but no matching registered connection.
    secret = "s"
    db = tmp_path / "webui.db"
    con = sqlite3.connect(db)
    con.execute('CREATE TABLE "user" (id TEXT PRIMARY KEY, email TEXT)')
    con.execute("CREATE TABLE oauth_session (id TEXT PRIMARY KEY, user_id TEXT, "
                "provider TEXT, token TEXT, expires_at BIGINT, created_at BIGINT, updated_at BIGINT)")
    con.execute("CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT, updated_at BIGINT)")
    con.execute('INSERT INTO "user" VALUES (?,?)', ("u1", "d@x.org"))
    now = int(time.time())
    enc = _fernet(secret).encrypt(json.dumps({"access_token": "AT"}).encode()).decode()
    con.execute("INSERT INTO oauth_session VALUES (?,?,?,?,?,?,?)",
                ("s1", "u1", tok.mcp_provider("gone"), enc, now + 3600, now, now))
    con.execute("INSERT INTO config VALUES (?,?,?)", ("tool_server.connections", "[]", now))
    con.commit()
    con.close()
    monkeypatch.setenv("HUBZOID_OWUI_DB", str(db))
    monkeypatch.setenv("WEBUI_SECRET_KEY", secret)
    tok._fernet_cache.clear()
    assert owui_mcp.per_user_specs(".", _ident("d@x.org")) == ({}, [])


def test_expired_token_is_skipped(tmp_path, monkeypatch):
    # An expired stored token is dropped (reconnect) rather than injected to 401.
    secret = "s"
    db = tmp_path / "webui.db"
    _seed(db, user_id="u1", email="e@x.org", server_id="srv1", url="https://m/mcp",
          token={"access_token": "AT", "expires_at": int(time.time()) - 10}, secret=secret)
    monkeypatch.setenv("HUBZOID_OWUI_DB", str(db))
    monkeypatch.setenv("WEBUI_SECRET_KEY", secret)
    tok._fernet_cache.clear()
    assert owui_mcp.per_user_specs(".", _ident("e@x.org")) == ({}, [])
