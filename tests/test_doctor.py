"""`hubzoid doctor`: stable check ids, JSON for scripts, read only."""
from __future__ import annotations

import json
import time

import pytest
from typer.testing import CliRunner

from hubzoid import cli
from hubzoid import doctor as doc

# Every id doctor can report. Ids are only ever added: a rename breaks scripts.
STABLE_IDS = {
    "hub.dir", "hub.agents_md", "hub.env", "runtime.build", "schedule.tasks",
    "workflows.definitions", "access.restricted", "identity.resolver", "deps.versions", "deps.sqlite",
    "db.operational", "db.hub", "db.read", "auth.bridge_keys", "auth.chat_signin",
    "exposure.bind", "model.credentials", "backup.age", "scheduler.health",
    "config.layers", "secrets.deployment", "secrets.hub", "secrets.restricted",
    "secrets.names", "auth.google_merge",
}


@pytest.fixture
def hub(tmp_path, monkeypatch):
    h = tmp_path / "hub"
    h.mkdir()
    (h / "AGENTS.md").write_text("---\nname: hub\ndescription: d\nmodel: openrouter/anthropic/claude-haiku-4.5\n---\nbody")
    for k in ("HUBZOID_OPERATIONAL_DB", "HUBZOID_DBOS_DB", "DATABASE_URL", "HUBZOID_DEPLOYMENT",
              "MODEL", "WEBUI_AUTH", "WEBUI_SECRET_KEY", "HUBZOID_HOST", "BRIDGE_API_KEYS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    from hubzoid import access, migrations

    access._stores.clear()
    migrations._done.clear()
    return h


def _by_id(checks):
    return {c.id: c for c in checks}


def test_ids_are_stable_and_doctor_creates_nothing(hub):
    checks = doc.run(hub)
    assert {c.id for c in checks} <= STABLE_IDS
    assert {c.status for c in checks} <= {"ok", "info", "warn", "fail"}
    c = _by_id(checks)
    assert c["db.operational"].status == "info" and "Not created yet" in c["db.operational"].summary
    assert not (hub / ".hubzoid" / "hub.db").exists()  # read only
    assert c["model.credentials"].status == "ok"


def test_unsafe_settings_fail(hub, monkeypatch):
    c = _by_id(doc.run(hub))
    assert c["auth.bridge_keys"].status == "fail"  # unset means the public key 'dev'

    monkeypatch.setenv("BRIDGE_API_KEYS", "dev,other-key-long-enough")
    assert _by_id(doc.run(hub))["auth.bridge_keys"].status == "fail"
    monkeypatch.setenv("BRIDGE_API_KEYS", "short")
    assert _by_id(doc.run(hub))["auth.bridge_keys"].status == "warn"
    monkeypatch.setenv("BRIDGE_API_KEYS", "a-properly-long-random-key")
    assert _by_id(doc.run(hub))["auth.bridge_keys"].status == "ok"

    monkeypatch.setenv("HUBZOID_HOST", "0.0.0.0")
    c = _by_id(doc.run(hub))
    assert c["exposure.bind"].status == "warn" and c["auth.chat_signin"].status == "fail"
    monkeypatch.setenv("WEBUI_AUTH", "true")
    assert _by_id(doc.run(hub))["auth.chat_signin"].status == "fail"  # no secret
    monkeypatch.setenv("WEBUI_SECRET_KEY", "x" * 40)
    assert _by_id(doc.run(hub))["auth.chat_signin"].status == "ok"

    monkeypatch.delenv("OPENROUTER_API_KEY")
    c = _by_id(doc.run(hub))["model.credentials"]
    assert c.status == "fail" and "OPENROUTER_API_KEY" in c.summary


def test_schema_backup_and_schedule_checks(hub, tmp_path):
    from hubzoid import backup as bk
    from hubzoid.access import store_for

    (hub / "schedule").mkdir()
    (hub / "schedule" / "daily.md").write_text('---\nschedule: "0 3 * * *"\nrun: "true"\n---\n\nx\n')
    gs = store_for(hub)  # creates and upgrades the operational store
    c = _by_id(doc.run(hub))
    assert c["db.operational"].status == "ok"
    assert c["backup.age"].status == "warn"
    assert c["scheduler.health"].status == "ok"

    gs.set_workflow_paused("hub", "md:daily", True, actor="test")
    assert _by_id(doc.run(hub))["scheduler.health"].detail["paused"] == ["md:daily"]

    bk.backup(hub, tmp_path / "b.tar.gz", wait=0)
    c = _by_id(doc.run(hub))["backup.age"]
    assert c.status == "ok" and c.detail["path"] == str(tmp_path / "b.tar.gz")

    gs.set_metadata("backup:last", {"at": time.time() - 30 * 86400, "path": "old"})
    assert _by_id(doc.run(hub))["backup.age"].status == "warn"


def test_unknown_schema_revision_fails(hub):
    from sqlalchemy import text

    from hubzoid.access import store_for

    gs = store_for(hub)
    with gs._engine.begin() as c:
        c.execute(text("UPDATE hz_alembic_operational SET version_num='op_9999'"))
    c = _by_id(doc.run(hub))["db.operational"]
    assert c.status == "fail" and "newer" in c.summary


def test_json_output_and_exit_code(hub, monkeypatch):
    r = CliRunner().invoke(cli.app, ["doctor", str(hub), "--json"])
    assert r.exit_code == 1  # the bridge key is unset
    body = json.loads(r.output)
    assert body["format"] == 1 and body["ok"] is False
    assert {"id", "status", "summary", "detail"} <= set(body["checks"][0])

    monkeypatch.setenv("BRIDGE_API_KEYS", "a-properly-long-random-key")
    r = CliRunner().invoke(cli.app, ["doctor", str(hub), "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["ok"] is True

    r = CliRunner().invoke(cli.app, ["doctor", str(hub / "missing"), "--json"])
    assert r.exit_code == 2 and json.loads(r.output)["checks"][0]["id"] == "hub.dir"


def test_old_sqlite_on_python_312_is_a_failure(hub, monkeypatch):
    """DBOS needs unixepoch('subsec') (SQLite 3.42) on Python 3.12; Debian 12's
    Python links SQLite 3.40, where the engine silently fails to start."""
    import sqlite3
    import sys

    from hubzoid.workflows import runtime

    monkeypatch.setattr(sys, "version_info", (3, 12, 3, "final", 0))
    monkeypatch.setattr(sqlite3, "sqlite_version", "3.40.1")
    c = _by_id(doc.run(hub))["deps.sqlite"]
    assert c.status == "fail" and "3.42" in c.summary
    assert not runtime._INITED
    with pytest.raises(RuntimeError, match="needs SQLite 3.42"):
        runtime.init(hub)                        # refused before DBOS is touched
    assert not runtime._INITED
    assert runtime.sqlite_problem("postgresql+psycopg://h/db") is None
    monkeypatch.setattr(sqlite3, "sqlite_version", "3.46.1")
    assert runtime.sqlite_problem("sqlite:///x") is None
