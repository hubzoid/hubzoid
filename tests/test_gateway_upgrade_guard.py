"""A gateway that ran Open WebUI (1.0.x) keeps its upgrade guard after a start in
local mode (review 6, finding 2).

Starting such a gateway with sign-in off is allowed (a notice says old chats
can be imported). Opening the app then creates the local owner,
`admin@localhost`. That account must not count as a migrated one: once sign-in
is turned on, nobody could sign in with it, so the gateway must still stop and
point at `hubzoid migrate openwebui`. The local start must also keep the
manifest's record of where Open WebUI's database is, so that command (and the
guard) still find it.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

import hubzoid.access as access
from hubzoid import cli, deployment, secretbox
from hubzoid.auth import sessions
from tests._fake_secrets import clean_process_env


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    def fake_popen(cmd, *a, env=None, **kw):
        proc = MagicMock()
        proc.poll.return_value = None
        return proc

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(cli, "_wait_for", lambda *a, **k: True)
    monkeypatch.setattr(cli, "_ensure_port_available", lambda *a: None)
    monkeypatch.setattr(cli, "_wait_any", lambda procs, **k: None)
    monkeypatch.setattr(cli, "_stop_groups", lambda procs, **k: None)
    monkeypatch.setattr(cli.signal, "signal", lambda *a, **k: None)
    with clean_process_env():
        access._stores.clear()
        secretbox.reset_cache()
        sessions.reset_cache()
        yield
        access._stores.clear()
        secretbox.reset_cache()
        sessions.reset_cache()


def _hub(root: Path) -> Path:
    hub = root / "sales"
    hub.mkdir()
    (hub / "AGENTS.md").write_text("---\nname: sales\ndescription: The sales agent\n---\nbody")
    (hub / ".env").write_text("BRIDGE_PORT=4166\nMODEL=claude-local\n")
    return hub


def _legacy_gateway(tmp_path: Path, *, owui_database_url: str | None = None):
    """The data folder a 1.0.x gateway left: its manifest and, unless Open WebUI
    used a database server, Open WebUI's SQLite database with one person."""
    hub = _hub(tmp_path)
    gw = tmp_path / "gw"
    gw.mkdir()
    webui_db = gw / "webui.db"
    if owui_database_url is None:
        con = sqlite3.connect(webui_db)
        con.execute('CREATE TABLE "user" (id TEXT PRIMARY KEY, email TEXT, name TEXT, role TEXT)')
        con.execute("INSERT INTO \"user\" VALUES ('p1', 'person@example.org', 'Person', 'admin')")
        con.commit()
        con.close()
    deployment.save(gw / "deployment.json",
                    hubs=[dict(key="sales", name="sales", path=str(hub), model_id="sales", slug="sales")],
                    operational_url=f"sqlite:///{gw / 'hubzoid-operational.db'}",
                    owui_url="http://127.0.0.1:43080", owui_db=str(webui_db),
                    owui_database_url=owui_database_url or f"sqlite:///{webui_db}",
                    owui_database_schema="owui" if owui_database_url else None)
    return hub, gw


def _gateway(hub: Path, gw: Path, **env):
    with clean_process_env(**env):
        return CliRunner().invoke(cli.app, ["gateway", str(hub), "--port", "4167", "--data-dir", str(gw)],
                                  catch_exceptions=False)


def _open_the_app_in_local_mode(hub: Path) -> None:
    from hubzoid.auth.users import ensure_local_owner

    with clean_process_env():
        assert ensure_local_owner(hub)["email"] == "admin@localhost"


def test_the_local_owner_does_not_count_as_a_migrated_account(tmp_path):
    hub, gw = _legacy_gateway(tmp_path)
    result = _gateway(hub, gw)                          # local mode: a notice, and it starts
    assert result.exit_code == 0 and "migrate openwebui" in result.output
    _open_the_app_in_local_mode(hub)
    result = _gateway(hub, gw, HUBZOID_AUTH="true")
    assert result.exit_code == 1 and "hubzoid migrate openwebui" in result.output
    # Once a person has a Hubzoid account, sign-in mode starts.
    con = sqlite3.connect(gw / "hubzoid-operational.db")
    con.execute("INSERT INTO hz_users (id, email, role, status, source, created_at, updated_at) "
                "VALUES ('p1', 'person@example.org', 'admin', 'active', 'migrated', 0, 0)")
    con.commit()
    con.close()
    assert _gateway(hub, gw, HUBZOID_AUTH="true").exit_code == 0


def test_a_local_start_keeps_where_open_webui_kept_its_data(tmp_path):
    from hubzoid import migrate_openwebui

    hub, gw = _legacy_gateway(tmp_path)
    assert _gateway(hub, gw).exit_code == 0
    manifest = json.loads((gw / "deployment.json").read_text())
    assert manifest["ui_mode"] == "hubzoid" and manifest["owui_url"] == ""   # no Open WebUI runs
    assert manifest["owui_db"] == str(gw / "webui.db")
    assert manifest["owui_database_url"] == f"sqlite:///{gw / 'webui.db'}"
    # The documented next step finds the gateway's Open WebUI database.
    with clean_process_env():
        for where in (gw, hub):
            setup = migrate_openwebui.locate(where)
            assert Path(setup.source_url.database) == (gw / "webui.db").resolve()


def test_a_database_server_stays_on_record_after_a_local_start(tmp_path):
    url = "postgresql://hz@db.example.org/owui"
    hub, gw = _legacy_gateway(tmp_path, owui_database_url=url)
    op = f"sqlite:///{gw / 'hubzoid-operational.db'}"
    assert _gateway(hub, gw, HUBZOID_OPERATIONAL_DB=op).exit_code == 0
    manifest = json.loads((gw / "deployment.json").read_text())
    assert manifest["owui_database_url"] == url and manifest["owui_database_schema"] == "owui"
    _open_the_app_in_local_mode(hub)
    result = _gateway(hub, gw, HUBZOID_OPERATIONAL_DB=op, HUBZOID_AUTH="true")
    assert result.exit_code == 1 and "hubzoid migrate openwebui" in result.output


def test_a_new_web_app_gateway_records_no_open_webui(tmp_path):
    hub = _hub(tmp_path)
    gw = tmp_path / "gw"
    assert _gateway(hub, gw, HUBZOID_AUTH="true").exit_code == 0
    assert _gateway(hub, gw, HUBZOID_AUTH="true").exit_code == 0
    manifest = json.loads((gw / "deployment.json").read_text())
    assert manifest["owui_db"] == "" and manifest["owui_database_url"] is None
