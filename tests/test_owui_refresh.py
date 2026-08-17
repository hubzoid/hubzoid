"""Token refresh: keep a per-user MCP token fresh, write it back, fail closed.

Seeds an OWUI-shaped DB (oauth_session + tool_server.connections with an
encrypted client-info blob) and exercises every branch of access_token_for with
the HTTP POST mocked.
"""
from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import time

import pytest

from hubzoid import owui_refresh
from hubzoid.access import owui_client_info
from hubzoid.access import owui_oauth_tokens as tok
from hubzoid.access import owui_tool_servers as srv


def _fernet(secret):
    from cryptography.fernet import Fernet

    key = (base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
           if len(secret) != 44 else secret.encode())
    return Fernet(key)


def _seed(path, *, user_id, server_id, token, secret,
          token_endpoint="https://prov/token", client_id="cid", client_secret="csec"):
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE oauth_session (id TEXT PRIMARY KEY, user_id TEXT, "
                "provider TEXT, token TEXT, expires_at BIGINT, created_at BIGINT, updated_at BIGINT)")
    con.execute("CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT, updated_at BIGINT)")
    now = int(time.time())
    enc = _fernet(secret).encrypt(json.dumps(token).encode()).decode()
    con.execute("INSERT INTO oauth_session VALUES (?,?,?,?,?,?,?)",
                ("s1", user_id, tok.mcp_provider(server_id), enc, token.get("expires_at", now), now, now))
    ci = {"client_id": client_id, "client_secret": client_secret,
          "server_metadata": {"token_endpoint": token_endpoint}}
    ci_enc = _fernet(secret).encrypt(json.dumps(ci).encode()).decode()
    conn = {"type": "mcp", "url": "https://m/mcp", "auth_type": "oauth_2.1",
            "info": {"id": server_id, "name": "Prov", "oauth_client_info": ci_enc}, "config": {}}
    con.execute("INSERT INTO config VALUES (?,?,?)", ("tool_server.connections", json.dumps([conn]), now))
    con.commit()
    con.close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    secret = "shared-secret"
    db = tmp_path / "webui.db"
    monkeypatch.setenv("HUBZOID_OWUI_DB", str(db))
    monkeypatch.setenv("WEBUI_SECRET_KEY", secret)
    tok._fernet_cache.clear()
    return {"db": db, "secret": secret, "uid": "u1", "sid": "srv1"}


def test_valid_token_returned_without_refresh(env, monkeypatch):
    _seed(env["db"], user_id="u1", server_id="srv1", secret=env["secret"],
          token={"access_token": "AT-valid", "refresh_token": "RT", "expires_at": int(time.time()) + 3600})
    calls = []
    monkeypatch.setattr(owui_refresh, "_post_refresh", lambda *a, **k: calls.append(1))
    assert owui_refresh.access_token_for(".", "u1", "srv1") == "AT-valid"
    assert calls == []                                       # no refresh for a live token


def test_expired_refreshes_and_writes_back(env, monkeypatch):
    _seed(env["db"], user_id="u1", server_id="srv1", secret=env["secret"],
          token={"access_token": "AT-old", "refresh_token": "RT-old", "expires_at": int(time.time()) - 10})
    monkeypatch.setattr(owui_refresh, "_post_refresh",
                        lambda ci, rt, scope: {"access_token": "AT-new", "refresh_token": "RT-new", "expires_in": 3600})
    assert owui_refresh.access_token_for(".", "u1", "srv1") == "AT-new"
    sess = tok.read_session(".", "u1", "srv1")              # persisted back
    assert sess["token"]["access_token"] == "AT-new"
    assert sess["token"]["refresh_token"] == "RT-new"
    assert sess["token"]["expires_at"] > time.time()


def test_expired_no_refresh_token(env):
    _seed(env["db"], user_id="u1", server_id="srv1", secret=env["secret"],
          token={"access_token": "AT", "expires_at": int(time.time()) - 10})
    assert owui_refresh.access_token_for(".", "u1", "srv1") is None


def test_refresh_failure_leaves_old_token(env, monkeypatch):
    _seed(env["db"], user_id="u1", server_id="srv1", secret=env["secret"],
          token={"access_token": "AT", "refresh_token": "RT", "expires_at": int(time.time()) - 10})
    monkeypatch.setattr(owui_refresh, "_post_refresh", lambda *a, **k: None)
    assert owui_refresh.access_token_for(".", "u1", "srv1") is None
    assert tok.read_session(".", "u1", "srv1")["token"]["access_token"] == "AT"


def test_non_rotating_provider_keeps_refresh_token(env, monkeypatch):
    _seed(env["db"], user_id="u1", server_id="srv1", secret=env["secret"],
          token={"access_token": "AT", "refresh_token": "RT-keep", "expires_at": int(time.time()) - 10})
    monkeypatch.setattr(owui_refresh, "_post_refresh",
                        lambda *a, **k: {"access_token": "AT2", "expires_in": 3600})   # no refresh_token
    owui_refresh.access_token_for(".", "u1", "srv1")
    assert tok.read_session(".", "u1", "srv1")["token"]["refresh_token"] == "RT-keep"


def test_client_info_decrypts(env):
    _seed(env["db"], user_id="u1", server_id="srv1", secret=env["secret"],
          token={"access_token": "AT"}, token_endpoint="https://p/tok", client_id="CID", client_secret="SEC")
    ci = owui_client_info.client_info(".", "srv1")
    assert ci == {"token_endpoint": "https://p/tok", "client_id": "CID",
                  "client_secret": "SEC", "scope": None, "resource": None}


def test_filter_parsing():
    assert srv._parse_filter("a, b") == ["a", "b"]           # comma string (OWUI's format)
    assert srv._parse_filter(["x", "y"]) == ["x", "y"]
    assert srv._parse_filter("") is None
    assert srv._parse_filter(None) is None
    assert srv._parse_filter("a, !b") is None               # exclusion form -> no allow-list
