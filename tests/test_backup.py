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
    bk.backup(hub, tmp_path / "b.tar.gz", wait=30, poll=0.01, settle=0)
    assert seen["calls"] == 3 and all(seen["holds"])
    assert store_for(hub).schedule_hold() is None  # released afterwards

    # A run that never finishes: the backup gives up and still releases the hold.
    monkeypatch.setattr(bk, "running_runs", lambda plan, unknown=None: ["hub: md:daily:stuck"])
    with pytest.raises(bk.BackupError, match="md:daily:stuck"):
        bk.backup(hub, tmp_path / "c.tar.gz", wait=0.05, poll=0.01, settle=0)
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


def test_backup_waits_for_a_run_queued_as_the_hold_began(tmp_path, monkeypatch):
    """A scheduler that passed its hold check just before the hold still
    queues its run. The backup settles first, so it sees that run and waits."""
    hub = _standalone(tmp_path)
    run = {"state": "idle", "looks": 0}
    slept: list[float] = []

    def sleep(seconds):
        slept.append(seconds)
        if run["state"] == "idle":
            run["state"] = "queued"  # the scheduler finishes queuing meanwhile

    def running(plan, unknown=None):
        run["looks"] += 1
        if run["state"] == "queued" and run["looks"] > 1:
            run["state"] = "done"
        return ["hub: md:daily:late"] if run["state"] == "queued" else []

    written = []
    real_write = bk._write_archive
    monkeypatch.setattr(bk, "_write_archive", lambda *a: written.append(run["state"]) or real_write(*a))
    monkeypatch.setattr(bk.time, "sleep", sleep)
    monkeypatch.setattr(bk, "running_runs", running)
    bk.backup(hub, tmp_path / "b.tar.gz", wait=30, poll=0.01)
    assert written == ["done"] and slept[0] == bk._SETTLE

    # --wait 0 takes the backup at once, without settling.
    slept.clear()
    bk.backup(hub, tmp_path / "c.tar.gz", wait=0)
    assert slept == [] and (tmp_path / "c.tar.gz").exists()


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


def test_database_passwords_are_left_out_unless_asked(tmp_path, monkeypatch):
    a, b, gw = _gateway(tmp_path)
    manifest = json.loads((gw / "deployment.json").read_text())
    manifest["hubs"][0]["dbos_url"] = "postgresql+psycopg://hz:s3cret@127.0.0.1:1/dbos"
    manifest["hubs"][1]["dbos_url"] = "postgresql://hz@127.0.0.1:1/dbos?sslmode=require&password=s3cret"
    (gw / "deployment.json").write_text(json.dumps(manifest, indent=2))
    monkeypatch.setattr(bk, "running_runs", lambda plan, unknown=None: [])

    def holds_password(archive: Path) -> bool:
        with tarfile.open(archive) as t:
            return any(b"s3cret" in t.extractfile(m).read() for m in t.getmembers() if m.isfile())

    archive = tmp_path / "gw.tar.gz"
    index = bk.backup(a, archive, wait=0)
    assert not holds_password(archive) and len(index["redacted"]) == 1

    r = CliRunner().invoke(cli.app, ["restore", str(archive),
                                     "--move", f"{tmp_path / 'srv'}={tmp_path / 'new'}"])
    assert r.exit_code == 0, r.output
    ngw = tmp_path / "new" / "gateway-data"
    out = "".join(r.output.split())
    assert f"{ngw / 'deployment.json'}weresavedas***" in out and "startthegatewaybeforeanybridge" in out
    restored = json.loads((ngw / "deployment.json").read_text())
    assert restored["hubs"][0]["dbos_url"] == "postgresql+psycopg://hz:***@127.0.0.1:1/dbos"
    assert restored["hubs"][1]["dbos_url"].endswith("&password=***")
    assert restored["owui_db"] == str(ngw / "webui.db")  # paths are still moved

    bk.backup(a, tmp_path / "all.tar.gz", wait=0, include_secrets=True)
    assert holds_password(tmp_path / "all.tar.gz")


def test_postgres_is_named_not_copied(tmp_path, monkeypatch):
    hub = _hub(tmp_path)
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", "postgresql://hz:pw@db:5432/hubzoid")
    p = bk.plan(hub)
    assert p.external == ["postgresql://hz:***@db:5432/hubzoid"]


def _crafted(tmp_path: Path, roots: list[dict], files: dict[str, bytes], sqlite=()) -> Path:
    import io

    archive = tmp_path / "crafted.tar.gz"
    index = json.dumps({"format": 1, "roots": roots, "sqlite": list(sqlite)}).encode()
    with tarfile.open(archive, "w:gz") as t:
        for name, data in {**files, bk.INDEX: index}.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return archive


def test_unsafe_archives_are_refused(tmp_path):
    evil = _crafted(tmp_path, [{"id": "r0", "path": str(tmp_path / "hub" / "output"), "kind": "state"}],
                    {"../escape.txt": b"x"})
    with pytest.raises(bk.BackupError, match="unsafe"):
        bk.restore(evil)
    assert not (tmp_path.parent / "escape.txt").exists()


def _state(path: Path) -> dict:
    return {"id": "r0", "path": str(path), "kind": "state"}


_REFUSED = {
    "a directory backup never saves": lambda t: (
        [_state(t / "home" / ".ssh")], {"r0/authorized_keys": b"theirs"}, []),
    "a chat UI root with no chat UI data": lambda t: (
        [{"id": "r0", "path": str(t / "home" / ".ssh"), "kind": "ui"}], {"r0/authorized_keys": b"theirs"}, []),
    "an id outside the extraction directory": lambda t: (
        [{"id": str(t / "outside"), "path": str(t / "hub" / "output"), "kind": "state"}], {}, []),
    "a repeated id": lambda t: (
        [_state(t / "hub" / "output"), _state(t / "hub" / "logs")], {"r0/x": b"x"}, []),
    "an entry outside every saved location": lambda t: (
        [_state(t / "hub" / "output")], {"r1/x": b"x"}, []),
    "a database file that is not a database": lambda t: (
        [{"id": "r0", "path": str(t / "home" / ".bashrc"), "kind": "file"}], {"r0/.bashrc": b"theirs"},
        ["r0/.bashrc"]),
    "a relative path": lambda t: (
        [_state(Path("hub") / "output")], {"r0/x": b"x"}, []),
    "the filesystem root": lambda t: (
        [{"id": "r0", "path": "/", "kind": "ui"}], {"r0/webui.db": b"x"}, []),
    "the home directory": lambda t: (
        [{"id": "r0", "path": str(t / "home"), "kind": "ui"}], {"r0/webui.db": b"x"}, []),
    "one target inside another": lambda t: (
        [{"id": "r0", "path": str(t / "gw"), "kind": "ui"}, {"id": "r1", "path": str(t / "gw" / "output"),
                                                             "kind": "state"}],
        {"r0/webui.db": b"x", "r1/x": b"x"}, []),
}


@pytest.mark.parametrize("case", list(_REFUSED))
def test_restore_refuses_targets_backup_never_makes(tmp_path, monkeypatch, case):
    """The index is part of the archive, so it is checked like the entries:
    nothing is moved aside or replaced unless every target is one a backup
    could have saved."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "authorized_keys").write_text("mine")
    (home / ".bashrc").write_text("mine")
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "keep.txt").write_text("mine")
    monkeypatch.setenv("HOME", str(home))
    roots, files, sqlite = _REFUSED[case](tmp_path)
    archive = _crafted(tmp_path, roots, files, sqlite)
    with pytest.raises(bk.BackupError, match="Refusing"):
        bk.restore_plan(archive)  # --dry-run refuses too
    with pytest.raises(bk.BackupError, match="Refusing"):
        bk.restore(archive)
    assert (home / ".ssh" / "authorized_keys").read_text() == "mine"
    assert (home / ".bashrc").read_text() == "mine"
    assert (tmp_path / "outside" / "keep.txt").read_text() == "mine"
    assert not list(tmp_path.rglob("*pre-restore*"))


def test_cli_backup_and_dry_run_restore(tmp_path, monkeypatch):
    hub = _standalone(tmp_path)
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(cli.app, ["backup", str(hub), "--out", "b.tar.gz", "--wait", "0"])
    assert r.exit_code == 0, r.output
    assert "Backup written" in r.output
    assert "Left out: .env files, signing keys and database passwords" in " ".join(r.output.split())
    r = CliRunner().invoke(cli.app, ["restore", "b.tar.gz", "--dry-run",
                                     "--move", f"{tmp_path / 'live'}={tmp_path / 'elsewhere'}"])
    assert r.exit_code == 0, r.output
    assert str(tmp_path / "elsewhere" / "hub" / "output") in r.output.replace("\n", "")
    assert not (tmp_path / "elsewhere").exists()
    r = CliRunner().invoke(cli.app, ["restore", "b.tar.gz", "--move", "nonsense"])
    assert r.exit_code == 2
    assert os.path.exists("b.tar.gz")


def test_cli_backup_without_chat_data_says_so(tmp_path, monkeypatch):
    """A 0.9.x gateway hub has no pointer to its gateway before the first start,
    so its pre-upgrade backup finds no chat data. Say so; never claim accounts."""
    hub = _hub(tmp_path / "live")
    monkeypatch.chdir(tmp_path)
    r = CliRunner().invoke(cli.app, ["backup", str(hub), "--out", "b.tar.gz", "--wait", "0"])
    assert r.exit_code == 0, r.output
    out = " ".join(r.output.split())
    assert "No chat app data was found" in out
    assert "also archive the gateway's --data-dir" in out
    assert "holds user accounts" not in out


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
    # Startup diagnostics can fill an unread stderr pipe before QUEUED is
    # emitted. Use a file and a bounded readiness wait so failures cannot hang CI.
    log_path = tmp_path / "worker.log"
    worker_log = log_path.open("w")
    proc = subprocess.Popen([sys.executable, "-c", _SLOW, str(hub)], stdout=worker_log,
                            stderr=subprocess.STDOUT, text=True, env=dict(os.environ))
    worker_log.close()
    try:
        deadline = time.monotonic() + 45
        while "QUEUED" not in log_path.read_text() and time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            time.sleep(0.1)
        assert "QUEUED" in log_path.read_text(), log_path.read_text()
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
    bk.backup(hub, tmp_path / "old.tar.gz", wait=5, settle=0, say=said.append)
    tables = {r[0] for r in sqlite3.connect(hub / ".hubzoid" / "hub.db").execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"hz_meta", "hz_grants"}  # no Alembic table, nothing new
    assert any("could not be read" in line for line in said)
    keys = [r[0] for r in sqlite3.connect(hub / ".hubzoid" / "hub.db").execute("SELECT k FROM hz_meta")]
    assert keys == ["backup:last"]  # the hold was lifted
