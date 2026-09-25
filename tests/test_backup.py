"""`hubzoid backup` / `hubzoid restore`: one archive of a deployment's state,
taken while chat keeps working, restorable in place or at a new path."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tarfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hubzoid import backup as bk
from hubzoid import cli


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for k in ("HUBZOID_OPERATIONAL_DB", "HUBZOID_DBOS_DB", "DATABASE_URL", "HUBZOID_DEPLOYMENT"):
        monkeypatch.delenv(k, raising=False)
    from hubzoid import access, db, migrations

    access._stores.clear()
    migrations._done.clear()
    yield
    access._stores.clear()
    for eng in db._engines.values():
        eng.dispose()
    db._engines.clear()


def _owui_db(path: Path, upload: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE file (id TEXT PRIMARY KEY, path TEXT)")
    c.execute("CREATE TABLE user (id TEXT, email TEXT)")
    c.execute("INSERT INTO file VALUES ('f1', ?)", (str(upload),))
    c.execute("INSERT INTO user VALUES ('u1', 'a@example.org')")
    c.commit()
    c.close()


def _hub(root: Path, name: str = "hub") -> Path:
    hub = root / name
    (hub / "schedule").mkdir(parents=True)
    (hub / "AGENTS.md").write_text(f"---\nname: {name}\ndescription: d\n---\nbody")
    (hub / "schedule" / "daily.md").write_text('---\nschedule: "0 3 * * *"\nrun: "true"\n---\n\nx\n')
    (hub / ".env").write_text("BRIDGE_API_KEYS=secret-key\n")
    (hub / "output" / "s1").mkdir(parents=True)
    (hub / "output" / "s1" / "report.txt").write_text("report")
    (hub / "logs").mkdir()
    (hub / "logs" / "access.log").write_text("line\n")
    return hub


def _standalone(tmp_path: Path) -> Path:
    hub = _hub(tmp_path / "live")
    ui = hub / ".openwebui-data"
    (ui / "uploads").mkdir(parents=True)
    (ui / "uploads" / "f1_doc.txt").write_text("uploaded")
    (ui / "cache" / "embedding").mkdir(parents=True)
    (ui / "cache" / "embedding" / "model.bin").write_bytes(b"x" * 100)
    _owui_db(ui / "webui.db", ui / "uploads" / "f1_doc.txt")
    from hubzoid import scheduling as sch
    from hubzoid.access import store_for

    store_for(hub).grant("a@example.org", "hub", "use_hub", actor="test")
    sch.ScheduleState(hub).record_fired("daily", datetime(2026, 9, 24, 3), result="done")
    from hubzoid import _signing

    _signing._secret(hub)  # creates .hubzoid/artifact_secret
    return hub


def _names(archive: Path) -> list[str]:
    with tarfile.open(archive) as t:
        return t.getnames()


def test_backup_then_restore_at_a_new_path(tmp_path):
    hub = _standalone(tmp_path)
    archive = tmp_path / "b.tar.gz"
    index = bk.backup(hub, archive, wait=0)

    assert oct(archive.stat().st_mode & 0o777) == "0o600"
    names = _names(archive)
    assert not any(n.endswith((".env", "artifact_secret")) for n in names)  # no secrets by default
    assert not any("/cache/" in n for n in names)                          # no model cache
    assert any(n.endswith("output/s1/report.txt") or n.endswith("s1/report.txt") for n in names)
    assert len(index["sqlite"]) >= 2 and index["not_included"] == []
    import hubzoid

    assert bk.read_index(archive)["hubzoid"] == hubzoid.__version__

    new = tmp_path / "moved"
    result = bk.restore(archive, [(str(tmp_path / "live"), str(new))])
    assert result["kept"] == []
    h2 = new / "hub"
    assert (h2 / "output" / "s1" / "report.txt").read_text() == "report"
    assert (h2 / ".openwebui-data" / "uploads" / "f1_doc.txt").read_text() == "uploaded"
    assert not (h2 / ".openwebui-data" / "cache").exists()
    (path,) = sqlite3.connect(h2 / ".openwebui-data" / "webui.db").execute("SELECT path FROM file").fetchone()
    assert path == str(h2 / ".openwebui-data" / "uploads" / "f1_doc.txt")

    # The restored operational store has the grants and no leftover hold.
    from hubzoid import access

    access._stores.clear()
    (h2 / "AGENTS.md").write_text("---\nname: hub\ndescription: d\n---\nbody")
    store = access.store_for(h2)
    assert store.schedule_hold() is None
    assert ("a@example.org", "hub", "use_hub") in store.list_grants("hub")


def test_secrets_only_when_asked(tmp_path):
    hub = _standalone(tmp_path)
    archive = tmp_path / "b.tar.gz"
    bk.backup(hub, archive, wait=0, include_secrets=True)
    names = _names(archive)
    assert any(n.endswith("/.env") for n in names)
    assert any(n.endswith("artifact_secret") for n in names)


def test_restore_in_place_keeps_the_current_state_aside(tmp_path):
    hub = _standalone(tmp_path)
    archive = tmp_path / "b.tar.gz"
    bk.backup(hub, archive, wait=0)
    (hub / "output" / "s1" / "report.txt").write_text("changed after the backup")
    for leftover in hub.rglob("*.db-*"):
        leftover.unlink()  # the hub is stopped
    result = bk.restore(archive)
    assert (hub / "output" / "s1" / "report.txt").read_text() == "report"
    aside = [Path(p) for p in result["kept"] if Path(p).name.startswith("output.pre-restore-")]
    assert aside and (aside[0] / "s1" / "report.txt").read_text() == "changed after the backup"


def test_restore_refuses_a_database_in_use(tmp_path):
    hub = _standalone(tmp_path)
    archive = tmp_path / "b.tar.gz"
    bk.backup(hub, archive, wait=0)
    live = sqlite3.connect(hub / ".openwebui-data" / "webui.db")
    live.execute("PRAGMA journal_mode=WAL")
    live.execute("INSERT INTO user VALUES ('u2', 'b@example.org')")
    live.commit()
    try:
        with pytest.raises(bk.BackupError, match="in use"):
            bk.restore(archive)
    finally:
        live.close()


def test_backup_holds_new_runs_and_waits_for_running_ones(tmp_path, monkeypatch):
    hub = _standalone(tmp_path)
    from hubzoid import scheduler as scheduler_lib
    from hubzoid.access import store_for

    seen = {"holds": [], "calls": 0}

    def running(plan, unknown=None):
        seen["calls"] += 1
        seen["holds"].append(store_for(hub).schedule_hold() is not None)
        return ["hub: md:daily:x"] if seen["calls"] < 3 else []

    monkeypatch.setattr(bk, "running_runs", running)
    bk.backup(hub, tmp_path / "b.tar.gz", wait=30, poll=0.01)
    assert seen["calls"] == 3 and all(seen["holds"])
    assert store_for(hub).schedule_hold() is None  # released afterwards

    # A run that never finishes: the backup gives up and still releases the hold.
    monkeypatch.setattr(bk, "running_runs", lambda plan, unknown=None: ["hub: md:daily:stuck"])
    with pytest.raises(bk.BackupError, match="md:daily:stuck"):
        bk.backup(hub, tmp_path / "c.tar.gz", wait=0.05, poll=0.01)
    assert store_for(hub).schedule_hold() is None and not (tmp_path / "c.tar.gz").exists()

    # While a hold is on, the scheduler fires nothing; the due task fires after.
    fired = []
    s = scheduler_lib.Scheduler(hub, dispatch_task=lambda t, slot, c: fired.append(t.name))
    from hubzoid import scheduling as sch

    sch.ScheduleState(hub).record_fired("daily", datetime.now() - timedelta(days=2), result="done")
    store_for(hub).set_schedule_hold("backup", 60, actor="test")
    assert asyncio.run(s.check_once()) == []
    store_for(hub).clear_schedule_hold()
    assert asyncio.run(s.check_once()) == ["daily"]


def test_a_hold_expires_on_its_own(tmp_path):
    hub = _standalone(tmp_path)
    from hubzoid.access import store_for

    store_for(hub).set_schedule_hold("backup", -1, actor="test")
    assert store_for(hub).schedule_hold() is None


def test_code_workflow_dispatch_waits_out_a_hold(tmp_path, monkeypatch):
    hub = _standalone(tmp_path)
    from hubzoid.access import store_for
    from hubzoid.workflows import runtime

    monkeypatch.setattr(runtime, "_HUB_DIR", hub)
    monkeypatch.setattr(runtime, "_HUB_NAME", "hub")
    store_for(hub).set_schedule_hold("backup", 60, actor="test")
    now = datetime.now().astimezone()
    assert runtime.tick(last=now - timedelta(minutes=1), now=now) is None


def _gateway(tmp_path: Path) -> tuple[Path, Path, Path]:
    base = tmp_path / "srv"
    a, b = _hub(base / "repo", "alpha"), _hub(base / "repo", "beta")
    gw = base / "gateway-data"
    (gw / "uploads").mkdir(parents=True)
    (gw / "uploads" / "f1_x.txt").write_text("u")
    _owui_db(gw / "webui.db", gw / "uploads" / "f1_x.txt")
    from hubzoid import deployment

    deployment.save(gw / "deployment.json",
                    hubs=[dict(key=h.name, name=h.name, path=str(h), model_id=h.name,
                               dbos_url=f"sqlite:///{h}/.hubzoid/dbos.db") for h in (a, b)],
                    operational_url=f"sqlite:///{gw / 'hubzoid-operational.db'}",
                    owui_url="http://127.0.0.1:43080", owui_db=str(gw / "webui.db"))
    from hubzoid.access import store_for

    store_for(a).grant("a@example.org", "alpha", "use_hub", actor="test")
    return a, b, gw


def test_a_gateway_hub_backs_up_the_whole_gateway_and_moves(tmp_path):
    a, b, gw = _gateway(tmp_path)
    archive = tmp_path / "gw.tar.gz"
    index = bk.backup(a, archive, wait=0)
    paths = {r["path"] for r in index["roots"]}
    assert str(gw) in paths and str(b / "output") in paths

    bk.restore(archive, [(str(tmp_path / "srv"), str(tmp_path / "new"))])
    ngw = tmp_path / "new" / "gateway-data"
    manifest = json.loads((ngw / "deployment.json").read_text())
    assert manifest["operational_url"] == f"sqlite:///{ngw / 'hubzoid-operational.db'}"
    assert manifest["owui_db"] == str(ngw / "webui.db")
    assert {h["path"] for h in manifest["hubs"]} == {str(tmp_path / "new" / "repo" / n) for n in ("alpha", "beta")}
    pointer = json.loads((tmp_path / "new" / "repo" / "beta" / ".hubzoid" / "deployment.json").read_text())
    assert pointer["manifest"] == str(ngw / "deployment.json")
    (path,) = sqlite3.connect(ngw / "webui.db").execute("SELECT path FROM file").fetchone()
    assert path == str(ngw / "uploads" / "f1_x.txt")


def test_postgres_is_named_not_copied(tmp_path, monkeypatch):
    hub = _hub(tmp_path)
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", "postgresql://hz:pw@db:5432/hubzoid")
    p = bk.plan(hub)
    assert p.external == ["postgresql://hz:***@db:5432/hubzoid"]


def test_unsafe_archives_are_refused(tmp_path):
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(evil, "w:gz") as t:
        data = json.dumps({"format": 1, "roots": [{"id": "r0", "path": str(tmp_path / "x"), "kind": "state"}],
                           "sqlite": []}).encode()
        info = tarfile.TarInfo(bk.INDEX)
        info.size = len(data)
        import io

        t.addfile(info, io.BytesIO(data))
        info = tarfile.TarInfo("../escape.txt")
        info.size = 1
        t.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(bk.BackupError, match="unsafe"):
        bk.restore(evil)
    assert not (tmp_path.parent / "escape.txt").exists()


def test_cli_backup_and_dry_run_restore(tmp_path, monkeypatch):
    hub = _standalone(tmp_path)
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(cli.app, ["backup", str(hub), "--out", "b.tar.gz", "--wait", "0"])
    assert r.exit_code == 0, r.output
    assert "Backup written" in r.output and "Secrets were left out" in r.output
    r = CliRunner().invoke(cli.app, ["restore", "b.tar.gz", "--dry-run",
                                     "--move", f"{tmp_path / 'live'}={tmp_path / 'elsewhere'}"])
    assert r.exit_code == 0, r.output
    assert str(tmp_path / "elsewhere" / "hub" / "output") in r.output.replace("\n", "")
    assert not (tmp_path / "elsewhere").exists()
    r = CliRunner().invoke(cli.app, ["restore", "b.tar.gz", "--move", "nonsense"])
    assert r.exit_code == 2
    assert os.path.exists("b.tar.gz")


_SLOW = """
import sys, time
from hubzoid.workflows import markdown, runtime
runtime.init(sys.argv[1])
runtime.launch()
markdown.enqueue_task("slow", "s1")
print("QUEUED", flush=True)
time.sleep(90)
"""


@pytest.mark.skipif(os.name == "nt", reason="uses a background process")
def test_running_runs_sees_a_live_scheduled_run(tmp_path):
    import subprocess
    import sys
    import time

    hub = _hub(tmp_path)
    (hub / "schedule" / "slow.md").write_text('---\nschedule: "0 4 * * *"\nrun: "sleep 60"\n---\n\nx\n')
    proc = subprocess.Popen([sys.executable, "-c", _SLOW, str(hub)], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=dict(os.environ))
    try:
        assert proc.stdout.readline().startswith("QUEUED")
        p = bk.plan(hub)
        deadline = time.time() + 30
        busy = bk.running_runs(p)
        while not busy and time.time() < deadline:
            time.sleep(0.3)
            busy = bk.running_runs(p)
        assert busy == ["hub: md:slow:s1"]
    finally:
        proc.kill()
        proc.wait(timeout=30)


def test_backing_up_an_older_release_changes_no_schema(tmp_path):
    """The upgrade guide backs up with the new release before its first start.
    The copy must be of the old databases as they are: nothing is migrated."""
    hub = _hub(tmp_path / "live")
    (hub / ".hubzoid").mkdir()
    old = sqlite3.connect(hub / ".hubzoid" / "hub.db")      # a 0.9.x operational store
    old.execute("CREATE TABLE hz_meta (k TEXT PRIMARY KEY, v TEXT)")
    old.execute("CREATE TABLE hz_grants (subject TEXT, hub TEXT, permission TEXT)")
    old.commit()
    old.close()
    dbos = sqlite3.connect(hub / ".hubzoid" / "dbos.db")     # a DBOS database this client can't read
    dbos.execute("CREATE TABLE something_else (x)")
    dbos.commit()
    dbos.close()
    said = []
    bk.backup(hub, tmp_path / "old.tar.gz", wait=5, say=said.append)
    tables = {r[0] for r in sqlite3.connect(hub / ".hubzoid" / "hub.db").execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"hz_meta", "hz_grants"}  # no Alembic table, nothing new
    assert any("could not be read" in line for line in said)
    keys = [r[0] for r in sqlite3.connect(hub / ".hubzoid" / "hub.db").execute("SELECT k FROM hz_meta")]
    assert keys == ["backup:last"]  # the hold was lifted
