"""Connector file refusals, legacy decoding and dynamic-registration state."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from hubzoid import connectors, secretbox
from hubzoid.connectors import ConnectorError, registry
from tests import connectors_fakes as f
from tests.test_connectors_registry import body, hub  # noqa: F401


@pytest.mark.parametrize("data,code", [(None, "invalid_file"), ({"extra": True}, "invalid_file"),
    ({"sign_in": "shared"}, "invalid_shared_secret"), ({"hubs": "sales"}, "invalid_hubs"),
    ({"hubs": [""]}, "invalid_hubs"), ({"client_id": 3}, "invalid_client_id"),
    ({"client_id": "x" * 513}, "invalid_client_id"), ({"client_id": "a\x00b"}, "invalid_client_id"),
    ({"client_secret": "raw secret"}, "invalid_ref"), ({"scopes": {}}, "invalid_scopes"),
    ({"tools": {}}, "invalid_tool_allowlist"), ({"tools": ["ok"] * 201}, "invalid_tool_allowlist")])
def test_document_validation(hub, data, code):
    with pytest.raises(ConnectorError) as exc:
        registry.parse_document(hub / "mail.yaml", {"url": "https://example.com/mcp", **data} if isinstance(data, dict) else data)
    assert exc.value.code == code and exc.value.status == 422


@pytest.mark.parametrize("name,content", [("mail.yaml", "[broken"), ("Mail-Bad.yaml", "version: 1\nurl: https://example.com/mcp")])
def test_file_errors_name_the_file(hub, name, content):
    path = hub / name
    path.write_text(content)
    with pytest.raises(ConnectorError) as exc:
        registry.parse_file(path)
    assert name in exc.value.message
    assert exc.value.code == ("invalid_file" if name == "mail.yaml" else "invalid_id")


def test_invalid_first_definition_is_cached_as_unavailable(hub):
    path = registry.folder(hub) / "mail.yaml"
    path.parent.mkdir()
    path.write_text("version: 1\nurl: invalid")
    assert registry.definitions(hub) == {}
    assert registry.definitions(hub) == {}
    assert registry.get(hub, "not valid") is None
    assert registry.client_secret(hub, "missing") is None
    assert registry.agents_of(hub, "missing") == []


def test_secret_resolution_refuses_missing_and_header_injection(hub, monkeypatch):
    f.define(hub, body(id="mail", client_id="id", client_secret="secret"))
    monkeypatch.delenv("HZ_TEST_MAIL_CLIENT_SECRET")
    with pytest.raises(ConnectorError) as exc:
        registry.client_secret(hub, "mail")
    assert exc.value.code == "secret_unreadable"
    f.define(hub, body(id="shared", auth_type="shared", shared_secret="key"))
    monkeypatch.setenv("HZ_TEST_SHARED_SHARED_KEY", "key\r\ninjected: value")
    assert registry.shared_headers(hub, "shared") is None
    assert registry._resolve(hub, None, "missing") is None
    assert registry._expired({"client_secret_expires_at": "invalid"})


def test_registration_missing_and_exhausted_attempts(hub):
    assert registry.registration(hub, "missing", "https://callback") is None
    with pytest.raises(ConnectorError) as exc:
        registry.save_registration(hub, "missing", "https://callback", {})
    assert exc.value.code == "not_found"
    with pytest.raises(ConnectorError) as exc:
        registry.save_registration(hub, "missing", "https://callback", {}, attempts=0)
    assert exc.value.code == "busy"
    assert registry._registrations(hub, "bad ciphertext") == {}


def test_legacy_definitions_decrypt_only_for_migration(hub):
    eng = connectors.engine(hub)
    encrypted = secretbox.encrypt(hub, "client-secret")
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO hz_connectors (id,name,url,auth_type,enabled,created_at,updated_at,"
            "created_by,client_secret_enc,shared_secret_enc,tool_allowlist) "
            "VALUES ('mail','Mail','https://example.com/mcp','oauth',1,1,1,'admin',:secret,'invalid','invalid')"),
            {"secret": encrypted})
    # Migration 0080 dropped the agent list: with the table gone, no hubs.
    rows = registry.stored_definitions(hub)
    assert rows[0]["client_secret"] == "client-secret" and rows[0]["shared_secret"] is None
    assert rows[0]["hubs"] == []
    # A database from before 2.0 still has it (created here as 0016 did).
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE hz_connector_agents (connector_id VARCHAR(64), hub VARCHAR(255))"))
        conn.execute(text("INSERT INTO hz_connector_agents (connector_id,hub) VALUES ('mail','sales')"))
    rows = registry.stored_definitions(hub)
    assert rows[0]["hubs"] == ["sales"]
    row = {key: None for key in registry._COLUMNS}
    row.update(id="mail", name="Mail", url="https://example.com", enabled=1,
               tool_allowlist="invalid")
    decoded = registry._connector(row)
    assert decoded.tool_allowlist == ("",) and decoded.auth_type == "oauth"
    row["tool_allowlist"] = '["search"]'
    assert registry._connector(row).tool_allowlist == ("search",)


def test_existing_store_never_creates_database(hub, monkeypatch):
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", "sqlite:///:memory:")
    assert connectors.existing_engine(hub) is None
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{hub / 'absent.db'}")
    assert connectors.existing_engine(hub) is None and not (hub / "absent.db").exists()
    eng = connectors.engine(hub)
    assert connectors.existing_engine(hub) is eng
