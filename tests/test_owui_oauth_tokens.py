"""Hubzoid reads + decrypts OWUI's per-user MCP OAuth tokens.

Seeds a webui.db shaped exactly like Open WebUI 0.11's (`user` +
`oauth_session`), encrypts a token the way OWUI does, and proves the reader
round-trips it and fails closed on every degraded input.
"""
from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import time

import pytest

from hubzoid.access import owui_oauth_tokens as tok


def _owui_fernet(secret: str):
    """Build a Fernet key the way OWUI's OAuthSessionTable does."""
    from cryptography.fernet import Fernet

    if len(secret) != 44:
        key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
    else:
        key = secret.encode()
    return Fernet(key)


def _seed_db(path, *, user_id, email, server_id, token: dict, secret: str):
    """Write an OWUI-shaped DB with one user and one encrypted MCP session."""
    con = sqlite3.connect(path)
    con.execute('CREATE TABLE "user" (id TEXT PRIMARY KEY, email TEXT)')
    con.execute(
        "CREATE TABLE oauth_session (id TEXT PRIMARY KEY, user_id TEXT, "
        "provider TEXT, token TEXT, expires_at BIGINT, created_at BIGINT, "
        "updated_at BIGINT)"
    )
    con.execute('INSERT INTO "user" VALUES (?, ?)', (user_id, email))
    enc = _owui_fernet(secret).encrypt(json.dumps(token).encode()).decode()
    now = int(time.time())
    con.execute(
        "INSERT INTO oauth_session VALUES (?,?,?,?,?,?,?)",
        (
            "sess-1", user_id, tok.mcp_provider(server_id), enc,
            token.get("expires_at", now + 3600), now, now,
        ),
    )
    con.commit()
    con.close()


@pytest.fixture
def owui(tmp_path, monkeypatch):
    """A seeded OWUI DB + env, returning the fixture facts for assertions."""
    secret = "test-secret-not-44-chars"  # exercises the sha256 branch
    db = tmp_path / "webui.db"
    facts = dict(
        user_id="u-123", email="Alice@Example.org", server_id="srv-odoo",
        token={"access_token": "at-abc", "refresh_token": "rt-xyz",
               "expires_at": int(time.time()) + 3600, "scope": "read"},
        secret=secret, db=db,
    )
    _seed_db(db, user_id=facts["user_id"], email=facts["email"],
             server_id=facts["server_id"], token=facts["token"], secret=secret)
    monkeypatch.setenv("HUBZOID_OWUI_DB", str(db))
    monkeypatch.setenv("WEBUI_SECRET_KEY", secret)
    tok._fernet_cache.clear()
    return facts


def test_resolve_user_id_is_case_insensitive(owui):
    # OWUI lowercases signups; a forwarded email may differ in case.
    assert tok.resolve_user_id(".", "alice@example.org") == owui["user_id"]
    assert tok.resolve_user_id(".", owui["email"]) == owui["user_id"]


def test_read_token_round_trips(owui):
    got = tok.read_token(".", owui["user_id"], owui["server_id"])
    assert got is not None
    assert got["access_token"] == "at-abc"
    assert got["refresh_token"] == "rt-xyz"


def test_connected_server_ids(owui):
    assert tok.connected_server_ids(".", owui["user_id"]) == {owui["server_id"]}


def test_wrong_secret_denies(owui, monkeypatch):
    monkeypatch.setenv("WEBUI_SECRET_KEY", "a-different-secret")
    tok._fernet_cache.clear()
    assert tok.read_token(".", owui["user_id"], owui["server_id"]) is None


def test_unknown_user_and_server_deny(owui):
    assert tok.read_token(".", "nobody", owui["server_id"]) is None
    assert tok.read_token(".", owui["user_id"], "srv-unknown") is None
    assert tok.resolve_user_id(".", "ghost@example.org") is None


def test_missing_db_denies(tmp_path, monkeypatch):
    monkeypatch.setenv("HUBZOID_OWUI_DB", str(tmp_path / "nope.db"))
    monkeypatch.setenv("WEBUI_SECRET_KEY", "whatever")
    assert tok.read_token(".", "u-123", "srv-odoo") is None
    assert tok.connected_server_ids(".", "u-123") == set()
