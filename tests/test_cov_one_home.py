"""`hubzoid upgrade` (hubzoid.one_home): settings kept only in the database
are written into their files before the database drops them. Connectors
defined in Settings become connectors/<id>.yaml with their secrets moved to
.env by name, and .env is only ever appended to, never overwritten."""
from __future__ import annotations

import os
import stat
import time

from sqlalchemy import create_engine, text
from typer.testing import CliRunner

from hubzoid import cli, one_home, secretbox
from tests.test_one_home_upgrade import _to, hub  # noqa: F401


def test_append_env_adds_only_missing_names_and_quotes_what_needs_it(tmp_path):
    env = tmp_path / ".env"
    assert one_home._append_env(env, {"A": "plain-value_1", "B": "has space", "EMPTY": ""}) == ["A", "B"]
    assert stat.S_IMODE(env.stat().st_mode) == 0o600            # a new file is private
    body = env.read_text()
    assert "A=plain-value_1\n" in body and 'B="has space"\n' in body and "EMPTY" not in body
    assert body.startswith("# moved here by `hubzoid upgrade`")

    env.write_text(env.read_text() + "C=kept")                  # no final newline
    assert one_home._append_env(env, {"A": "other", "C": "changed", "D": "new"}) == ["D"]
    lines = env.read_text().splitlines()
    assert "C=kept" in lines and "D=new" in lines and "A=other" not in lines
    assert one_home._append_env(env, {"A": "again"}) == []


def test_settings_connectors_become_files_with_secrets_in_env(hub, monkeypatch):  # noqa: F811
    eng = create_engine(f"sqlite:///{hub / '.hubzoid' / 'hub.db'}")
    _to(eng, "op_0071")
    now = time.time()
    with eng.begin() as c:
        c.execute(text(
            "INSERT INTO hz_connectors (id, name, url, auth_type, enabled, created_at, updated_at, created_by, "
            "client_id, scopes, client_secret_enc, shared_secret_enc, tool_allowlist, shared_header) VALUES "
            "('mail', 'Mail', 'https://mail.example.com/mcp', 'oauth', 0, :t, :t, 'admin', 'cid-1', 'read', "
            ":cs, NULL, :tools, NULL)"),
            {"t": now, "cs": secretbox.encrypt(hub, "client-secret-xyz"), "tools": '["search", "read"]'})
        c.execute(text("INSERT INTO hz_connector_agents (connector_id, hub, added_by, added_at) "
                       "VALUES ('mail', 'ops', 'admin', :t)"), {"t": now})

    dry = CliRunner().invoke(cli.app, ["upgrade", str(hub), "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert "connectors/mail.yaml" in dry.output and "HZ_CONNECTOR_MAIL_CLIENT_SECRET" in dry.output
    assert "client-secret-xyz" not in dry.output                 # names only, never values
    assert not (hub / "connectors" / "mail.yaml").exists()

    done = CliRunner().invoke(cli.app, ["upgrade", str(hub)])
    assert done.exit_code == 0, done.output
    import yaml

    doc = yaml.safe_load((hub / "connectors" / "mail.yaml").read_text())
    assert doc["url"] == "https://mail.example.com/mcp" and doc["sign_in"] == "oauth"
    assert doc["hubs"] == ["ops"] and doc["client_id"] == "cid-1" and doc["tools"] == ["search", "read"]
    assert doc["enabled"] is False and doc["client_secret"] == "env:HZ_CONNECTOR_MAIL_CLIENT_SECRET"
    assert "client-secret-xyz" not in (hub / "connectors" / "mail.yaml").read_text()
    assert "HZ_CONNECTOR_MAIL_CLIENT_SECRET=client-secret-xyz" in (hub / ".env").read_text()

    # Running it again changes nothing: the file exists, the name is already in .env.
    before = (hub / ".env").read_text()
    again = CliRunner().invoke(cli.app, ["upgrade", str(hub)])
    assert again.exit_code == 0 and (hub / ".env").read_text() == before
    assert os.stat(hub / ".env").st_size == len(before.encode())
