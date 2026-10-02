"""`hubzoid doctor` checks for the web app: the UI mode, sign-in, the
deployment key (fingerprint only), the openwebui extra in Open WebUI mode, an Open
WebUI install not yet moved, and the local-mode loopback guard."""
from __future__ import annotations

import importlib.util
import json
import sqlite3

import pytest
from cryptography.fernet import Fernet
from typer.testing import CliRunner

from hubzoid import cli, secretbox, webui
from hubzoid import doctor as doc

_ENV = ("HUBZOID_OPERATIONAL_DB", "HUBZOID_DBOS_DB", "DATABASE_URL", "HUBZOID_DEPLOYMENT", "MODEL",
        "WEBUI_AUTH", "WEBUI_SECRET_KEY", "HUBZOID_HOST", "HUBZOID_UI", "HUBZOID_AUTH",
        "HUBZOID_SECRET_KEY", "HUBZOID_ALLOW_UNAUTHENTICATED_NETWORK", "HUBZOID_ADMIN_EMAIL",
        "WEBUI_ADMIN_EMAIL", "HUBZOID_PUBLIC_URL", "WEBUI_URL", "HUBZOID_OWUI_DB")


@pytest.fixture
def hub(tmp_path, monkeypatch):
    h = tmp_path / "hub"
    h.mkdir()
    (h / "AGENTS.md").write_text("---\nname: hub\ndescription: d\nmodel: openrouter/anthropic/claude-haiku-4.5\n---\nbody")
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("BRIDGE_API_KEYS", "a-properly-long-random-key")
    from hubzoid import access, migrations

    access._stores.clear()
    migrations._done.clear()
    secretbox.reset_cache()
    yield h
    secretbox.reset_cache()


def _checks(hub) -> dict:
    return {c.id: c for c in doc.run(hub)}


def _owui_db(hub, people=3):
    path = hub / ".openwebui-data" / "webui.db"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as con:
        con.execute('CREATE TABLE "user" (id TEXT PRIMARY KEY, email TEXT)')
        for i in range(people):
            con.execute('INSERT INTO "user" VALUES (?, ?)', (f"u{i}", f"p{i}@example.com"))


def test_default_mode(hub):
    c = _checks(hub)
    assert c["ui.mode"].status == "info" and c["ui.mode"].summary == "Chat UI: Hubzoid web app"
    assert c["ui.mode"].detail["source"] == "default" and c["ui.mode"].detail["auth"] is False
    assert c["auth.chat_signin"].status == "info" and "local mode" in c["auth.chat_signin"].summary
    assert c["exposure.local_mode"].status == "ok"
    assert "ui.openwebui_extra" not in c and "ui.openwebui_data" not in c


def test_legacy_mode_needs_the_extra(hub, monkeypatch):
    monkeypatch.setenv("HUBZOID_UI", "openwebui")
    monkeypatch.setattr(webui, "_find_binary", lambda: None)
    c = _checks(hub)
    assert c["ui.mode"].summary == "Chat UI: Open WebUI (Open WebUI mode)"
    assert c["ui.openwebui_extra"].status == "fail"
    assert 'pip install "hubzoid[openwebui]"' in c["ui.openwebui_extra"].summary
    assert "exposure.local_mode" not in c  # legacy keeps the 1.0 auth.chat_signin check

    real = importlib.util.find_spec
    monkeypatch.setattr(webui, "_find_binary", lambda: "/venv/bin/open-webui")
    monkeypatch.setattr(importlib.util, "find_spec",
                        lambda name, *a, **k: object() if name == "open_webui" else real(name, *a, **k))
    assert _checks(hub)["ui.openwebui_extra"].status == "ok"


def test_open_webui_install_not_moved_yet(hub, monkeypatch):
    _owui_db(hub)
    c = _checks(hub)["ui.openwebui_data"]
    assert c.status == "info" and "hubzoid migrate openwebui" in c.summary  # local mode
    assert c.detail["openwebui_accounts"] == 3

    monkeypatch.setenv("HUBZOID_AUTH", "true")
    c = _checks(hub)["ui.openwebui_data"]
    assert c.status == "fail" and "hubzoid run stops" in c.summary

    from hubzoid.access import store_for

    store_for(hub)  # creates the operational store at the current schema
    from hubzoid import db
    from sqlalchemy import create_engine, inspect, text

    engine = create_engine(db.operational_url(hub))
    try:
        if not inspect(engine).has_table("hz_users"):
            pytest.skip("hz_users is created by migration op_0009")
        with engine.begin() as con:
            cols = {c["name"]: c for c in inspect(con).get_columns("hz_users")}
            values = {name: "x" for name, col in cols.items()
                      if not col.get("nullable", True) and col.get("default") is None}
            values.update(id="u0", email="p0@example.com")
            con.execute(text(f"INSERT INTO hz_users ({', '.join(values)}) VALUES "
                             f"({', '.join(':' + k for k in values)})"), values)
    finally:
        engine.dispose()
    assert "ui.openwebui_data" not in _checks(hub)  # moved: nothing to report


def test_local_mode_guard(hub, monkeypatch):
    monkeypatch.setenv("HUBZOID_HOST", "0.0.0.0")
    c = _checks(hub)["exposure.local_mode"]
    assert c.status == "fail" and "refuses to start" in c.summary
    monkeypatch.setenv("HUBZOID_ALLOW_UNAUTHENTICATED_NETWORK", "true")
    assert _checks(hub)["exposure.local_mode"].status == "warn"
    monkeypatch.setenv("HUBZOID_AUTH", "true")
    assert "exposure.local_mode" not in _checks(hub)


def test_sign_in_needs_a_first_administrator(hub, monkeypatch):
    monkeypatch.setenv("HUBZOID_AUTH", "true")
    c = _checks(hub)["auth.chat_signin"]
    assert c.status == "warn" and "HUBZOID_ADMIN_EMAIL" in c.summary
    monkeypatch.setenv("HUBZOID_ADMIN_EMAIL", "owner@example.com")
    c = _checks(hub)["auth.chat_signin"]
    assert c.status == "ok" and c.detail["mode"] == "accounts"


def test_deployment_key_is_reported_by_fingerprint_only(hub, monkeypatch):
    c = _checks(hub)["deployment.key"]
    assert c.status == "info" and "when first needed" in c.summary
    assert not (hub / ".hubzoid" / "secret.key").exists()  # doctor never creates it

    key = Fernet.generate_key().decode()
    monkeypatch.setenv("HUBZOID_SECRET_KEY", key)
    c = _checks(hub)["deployment.key"]
    assert c.status == "ok" and c.detail["source"] == "HUBZOID_SECRET_KEY"
    assert c.detail["fingerprint"] == secretbox.fingerprint(hub) and len(c.detail["fingerprint"]) == 12
    report = json.dumps(doc.report(hub, doc.run(hub)))
    assert key not in report

    monkeypatch.setenv("HUBZOID_SECRET_KEY", "not-a-fernet-key")
    assert _checks(hub)["deployment.key"].status == "fail"


def test_deployment_key_file(hub, monkeypatch):
    secretbox.keys(hub)  # what the bridge does at first use
    path = hub / ".hubzoid" / "secret.key"
    c = _checks(hub)["deployment.key"]
    assert c.status == "ok" and c.detail["path"] == str(path)
    assert path.read_text().strip() not in json.dumps(c.detail)
    path.chmod(0o644)
    assert _checks(hub)["deployment.key"].status == "warn"


def test_doctor_command_lists_the_new_checks(hub):
    r = CliRunner().invoke(cli.app, ["doctor", str(hub), "--json"])
    ids = {c["id"] for c in json.loads(r.output)["checks"]}
    assert {"ui.mode", "exposure.local_mode", "deployment.key"} <= ids
