"""The same identity/MCP contract against actual SQLite and PostgreSQL stores."""
from __future__ import annotations

import json
import time
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from hubzoid.access import owui_api_keys, owui_db, owui_groups, owui_oauth_tokens, owui_tool_servers


@pytest.fixture(params=["sqlite", "postgres", "postgres-schema"])
def owui_store(request, tmp_path, monkeypatch):
    monkeypatch.delenv("HUBZOID_DEPLOYMENT", raising=False)
    monkeypatch.delenv("DATABASE_SCHEMA", raising=False)
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'operational.db'}")
    monkeypatch.setenv("WEBUI_SECRET_KEY", "synthetic-test-encryption-secret")
    # A stale local DB must never win over DATABASE_URL (the gateway supplies
    # this path even when its shared OWUI runs against PostgreSQL).
    stale = tmp_path / "stale.db"
    from tests.test_mcp_server import _mk_owui_db
    _mk_owui_db(stale, key="stale-key", groups=("stale-group",))
    monkeypatch.setenv("HUBZOID_OWUI_DB", str(stale))
    schema = None
    database = None
    admin = None
    if request.param.startswith("postgres"):
        url = request.getfixturevalue("postgres_url")
        admin = create_engine(url, isolation_level="AUTOCOMMIT")
        if request.param == "postgres-schema":
            schema = "owui_" + uuid.uuid4().hex
            with admin.connect() as con:
                con.execute(text(f'CREATE SCHEMA "{schema}"'))
            engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
            monkeypatch.setenv("DATABASE_SCHEMA", schema)
        else:
            # Docker profile shape: DATABASE_URL alone, default public schema.
            database = "owui_" + uuid.uuid4().hex
            with admin.connect() as con:
                con.execute(text(f'CREATE DATABASE "{database}"'))
            url = make_url(url).set(database=database).render_as_string(hide_password=False)
            engine = create_engine(url)
    else:
        url = f"sqlite:///{tmp_path / 'actual.db'}"
        engine = create_engine(url)
    monkeypatch.setenv("DATABASE_URL", url)
    try:
        with engine.begin() as con:
            for ddl in (
                'CREATE TABLE "user" (id TEXT PRIMARY KEY, email TEXT, role TEXT)',
                'CREATE TABLE api_key (id TEXT PRIMARY KEY, user_id TEXT, "key" TEXT, expires_at BIGINT)',
                'CREATE TABLE "group" (id TEXT PRIMARY KEY, name TEXT)',
                'CREATE TABLE group_member (group_id TEXT, user_id TEXT)',
                'CREATE TABLE oauth_session (id TEXT PRIMARY KEY, user_id TEXT, provider TEXT, token TEXT, created_at BIGINT, updated_at BIGINT, expires_at BIGINT)',
                'CREATE TABLE config ("key" TEXT PRIMARY KEY, value JSON)',
            ):
                con.execute(text(ddl))
            con.execute(text('INSERT INTO "user" VALUES (:id, :email, :role)'), [
                {"id": "alice", "email": "alice@example.com", "role": "user"},
                {"id": "pending", "email": "pending@example.com", "role": "pending"},
            ])
            con.execute(text('INSERT INTO api_key VALUES (:id, :user_id, :key, :expiry)'), [
                {"id": "valid", "user_id": "alice", "key": "sk-test", "expiry": None},
                {"id": "expired", "user_id": "alice", "key": "expired", "expiry": int(time.time()) - 10},
                {"id": "pending", "user_id": "pending", "key": "pending", "expiry": None},
            ])
            con.execute(text('INSERT INTO "group" VALUES (:id, :name)'), {"id": "g", "name": "ClickUp"})
            con.execute(text("INSERT INTO group_member VALUES ('g', 'alice')"))
        yield tmp_path, engine
    finally:
        engine.dispose()
        if admin is not None:
            with admin.connect() as con:
                if database:
                    con.execute(text(f'DROP DATABASE "{database}"'))
                else:
                    con.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            admin.dispose()


def test_key_identity_and_denials(owui_store):
    hub, _ = owui_store
    assert owui_api_keys.resolve_email(hub, "sk-test") == "alice@example.com"
    for key in ("stale-key", "wrong", "expired", "pending", "", None):
        assert owui_api_keys.resolve_email(hub, key) is None
    assert owui_groups.resolve_groups(hub, "ALICE@EXAMPLE.COM") == {"clickup"}
    assert owui_groups.resolve_groups(hub, "unknown@example.com") == set()


def test_fresh_revocation_and_account_replacement(owui_store):
    hub, engine = owui_store
    assert owui_api_keys.resolve_email(hub, "sk-test") == "alice@example.com"
    with engine.begin() as con:
        con.execute(text("DELETE FROM group_member"))
    assert owui_groups.resolve_groups(hub, "alice@example.com") == set()
    with engine.begin() as con:
        con.execute(text('UPDATE "user" SET id=\'replacement\' WHERE id=\'alice\''))
        con.execute(text("UPDATE api_key SET user_id='replacement' WHERE user_id='alice'"))
    assert owui_api_keys.resolve_email(hub, "sk-test") is None


def test_suspended_identity_denied(owui_store):
    from hubzoid.access import store_for
    hub, _ = owui_store
    assert owui_api_keys.resolve_email(hub, "sk-test") == "alice@example.com"
    store_for(hub).suspend("alice@example.com", actor="test-admin")
    assert owui_api_keys.resolve_email(hub, "sk-test") is None


def test_read_connections_cannot_write(owui_store):
    hub, _ = owui_store
    con = owui_db.connect_ro(hub)
    assert con is not None
    with con:
        with pytest.raises(DBAPIError):
            con.execute(text("DELETE FROM api_key"))
    assert owui_api_keys.resolve_email(hub, "sk-test") == "alice@example.com"


def test_oauth_read_refresh_and_config(owui_store):
    hub, engine = owui_store
    token = {"access_token": "synthetic-original", "refresh_token": "synthetic-refresh"}
    encrypted = owui_oauth_tokens._fernet("synthetic-test-encryption-secret").encrypt(json.dumps(token).encode()).decode()
    connections = [{"type": "mcp", "url": "https://example.invalid/mcp", "auth_type": "oauth2", "info": {"id": "test-server"}}]
    with engine.begin() as con:
        con.execute(text("INSERT INTO oauth_session VALUES ('s', 'alice', 'mcp:test-server', :token, 1, 1, 9999999999)"), {"token": encrypted})
        con.execute(text("INSERT INTO config VALUES ('tool_server.connections', :value)"), {"value": json.dumps(connections)})
    assert owui_oauth_tokens.resolve_user_id(hub, "ALICE@example.com") == "alice"
    assert owui_oauth_tokens.read_token(hub, "alice", "test-server") == token
    assert owui_oauth_tokens.connected_server_ids(hub, "alice") == {"test-server"}
    assert owui_tool_servers.list_mcp_connections(hub)[0]["id"] == "test-server"
    assert owui_oauth_tokens.write_session(hub, "s", {"access_token": "synthetic-refreshed"})
    assert owui_oauth_tokens.read_session(hub, "alice", "test-server")["token"]["access_token"] == "synthetic-refreshed"
    with engine.connect() as con:
        assert "synthetic-refreshed" not in con.execute(text("SELECT token FROM oauth_session")).scalar_one()


def test_mcp_transport_group_and_key_revocation(owui_store, monkeypatch):
    from hubzoid import mcp_server
    from tests.test_mcp_server import _mk_hub, _call, _rpc, _result
    root, engine = owui_store
    hub = _mk_hub(root)
    monkeypatch.delenv("MCP_ACCESS_GROUP", raising=False)
    app = mcp_server.build_mcp_app(hub)
    body = _rpc("tools/call", {"name": "clickup_echo", "arguments": {"text": "hello"}})
    result = _result(_call(app, body))
    assert "clickup says: hello" in str(result)
    with engine.begin() as con:
        con.execute(text("DELETE FROM group_member"))
    result = _result(_call(app, body))
    assert "clickup says: hello" not in str(result)
    assert "denied" in str(result).lower()
    with engine.begin() as con:
        con.execute(text("DELETE FROM api_key"))
    assert _call(app, _rpc("tools/list")).status_code == 401


def test_mcp_entry_group(owui_store, monkeypatch):
    from hubzoid import mcp_server
    from tests.test_mcp_server import _mk_hub, _call, _rpc
    root, engine = owui_store
    hub = _mk_hub(root)
    monkeypatch.setenv("MCP_ACCESS_GROUP", "clickup")
    app = mcp_server.build_mcp_app(hub)
    assert _call(app, _rpc("tools/list")).status_code == 200
    with engine.begin() as con:
        con.execute(text("DELETE FROM group_member"))
    assert _call(app, _rpc("tools/list")).status_code == 401


def test_registered_database_and_conflicting_environment(owui_store, monkeypatch):
    from hubzoid import deployment
    hub, _ = owui_store
    url, schema = owui_db.database_config(hub)
    deployment.save(hub / "gateway.json", hubs=[{"key": "test", "path": str(hub)}],
                    operational_url=f"sqlite:///{hub / 'operational.db'}",
                    owui_url="http://localhost:9999", owui_db=str(hub / "stale.db"),
                    owui_database_url=url.render_as_string(hide_password=False), owui_database_schema=schema)
    monkeypatch.delenv("DATABASE_URL")
    monkeypatch.delenv("DATABASE_SCHEMA", raising=False)
    assert owui_api_keys.resolve_email(hub, "sk-test") == "alice@example.com"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{hub / 'stale.db'}")
    assert owui_api_keys.resolve_email(hub, "stale-key") is None
    assert owui_groups.resolve_groups(hub, "alice@example.com") == set()


def test_configured_unavailable_database_never_falls_back(tmp_path, monkeypatch, caplog):
    from tests.test_mcp_server import _mk_owui_db
    stale = _mk_owui_db(tmp_path / "stale.db", groups=("clickup",))
    monkeypatch.setenv("HUBZOID_OWUI_DB", str(stale))
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://synthetic:secret-canary@127.0.0.1:1/unavailable")
    assert owui_api_keys.resolve_email(tmp_path, "sk-test") is None
    assert owui_groups.resolve_groups(tmp_path, "alice@example.com") == set()
    assert "secret-canary" not in caplog.text
