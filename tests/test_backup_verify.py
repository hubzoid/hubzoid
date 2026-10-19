"""Verified backups (HUB-109): checksums and integrity checks, a failed backup
never counts, scheduled weekly backups, PostgreSQL dumps and a restore
rehearsal that touches nothing."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import sqlite3
import tarfile
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hubzoid import backup as bk
from hubzoid import backup_schedule as sched
from hubzoid import cli
from tests.test_backup import _standalone, clean  # noqa: F401 — clean is an autouse fixture


def _meta(hub: Path, key: str):
    from hubzoid import db

    url = db.operational_url(hub)
    conn = sqlite3.connect(url.split(":///", 1)[1])
    try:
        row = conn.execute("SELECT v FROM hz_meta WHERE k=?", (key,)).fetchone()
    finally:
        conn.close()
    return json.loads(row[0]) if row else None


def _repack(src: Path, dst: Path, change) -> None:
    """Copy an archive, passing each member's bytes through change(name, data)."""
    with tarfile.open(src, "r:gz") as old, tarfile.open(dst, "w:gz") as new:
        for m in old.getmembers():
            data = old.extractfile(m).read() if m.isfile() else None
            data = change(m.name, data) if data is not None else None
            if data is not None:
                m.size = len(data)
            new.addfile(m, io.BytesIO(data) if data is not None else None)


def test_every_backup_records_checksums_and_is_verified(tmp_path):
    hub = _standalone(tmp_path)
    archive = tmp_path / "b.tar.gz"
    index = bk.backup(hub, archive, wait=0)
    assert index["verified"]["ok"] and index["verified"]["databases"] == len(index["sqlite"]) >= 2
    with tarfile.open(archive) as tar:
        for name, digest in index["files"].items():
            assert hashlib.sha256(tar.extractfile(name).read()).hexdigest() == digest
    last = _meta(hub, "backup:last")
    assert last["verified"] is True and last["files"] == index["verified"]["files"]


def test_a_changed_or_cut_archive_fails_verification(tmp_path):
    hub = _standalone(tmp_path)
    good = tmp_path / "b.tar.gz"
    bk.backup(hub, good, wait=0)

    changed = tmp_path / "changed.tar.gz"
    _repack(good, changed, lambda name, data: data.replace(b"report", b"REPORT") if name.endswith("report.txt") else data)
    result = bk.verify(changed)
    assert not result["ok"] and any("report.txt: checksum does not match" in p for p in result["problems"])

    cut = tmp_path / "cut.tar.gz"
    cut.write_bytes(good.read_bytes()[: good.stat().st_size // 2])
    result = bk.verify(cut)
    assert not result["ok"] and result["problems"]


def test_a_damaged_database_fails_its_integrity_check(tmp_path):
    hub = _standalone(tmp_path)
    good = tmp_path / "b.tar.gz"
    index = bk.backup(hub, good, wait=0)
    target = index["sqlite"][0]
    broken: dict[str, str] = {}

    def damage(name, data):
        if name == target:
            data = data[:100] + b"\xff" * 4000 + data[4100:]
            broken["sha"] = hashlib.sha256(data).hexdigest()
        return data

    first = tmp_path / "first.tar.gz"
    _repack(good, first, damage)
    # The index's checksum matches the damaged file, so only the integrity check can catch it.
    index["files"][target] = broken["sha"]
    out = tmp_path / "damaged.tar.gz"
    _repack(first, out, lambda name, data: json.dumps(index).encode() if name == bk.INDEX else data)
    result = bk.verify(out)
    assert not result["ok"] and any(p.startswith(target) for p in result["problems"])
    assert not any("checksum" in p for p in result["problems"])


def test_a_backup_that_fails_verification_is_deleted_and_never_counts(tmp_path, monkeypatch):
    hub = _standalone(tmp_path)
    monkeypatch.setattr(bk, "verify", lambda archive: {"ok": False, "problems": ["a checksum does not match"]})
    archive = tmp_path / "b.tar.gz"
    with pytest.raises(bk.BackupError, match="did not pass verification"):
        bk.backup(hub, archive, wait=0)
    assert not archive.exists()
    assert _meta(hub, "backup:last") is None
    assert "did not pass verification" in _meta(hub, "backup:failed")["error"]

    from hubzoid import doctor

    row = doctor._backup_age(None, _meta(hub, "backup:failed"))
    assert row.status == "warn" and "failed" in row.summary


def test_scheduled_backups_are_weekly_verified_kept_and_pruned(tmp_path, monkeypatch):
    hub = _standalone(tmp_path)
    monkeypatch.setenv(sched.SETTING, "weekly")
    monkeypatch.setenv(sched.KEEP_SETTING, "2")
    assert sched.due(hub)                                       # never backed up
    names = iter(f"hubzoid-backup-2026101{n}-030000.tar.gz" for n in range(1, 4))
    real_backup = bk.backup
    monkeypatch.setattr(bk, "backup", lambda hub_dir, out, **kw: real_backup(
        hub_dir, out.with_name(next(names)), wait=0, **{k: v for k, v in kw.items() if k != "wait"}))
    for _ in range(3):
        sched.run_once(hub)
    kept = [a["name"] for a in sched.archives(hub)]
    assert kept == ["hubzoid-backup-20261013-030000.tar.gz", "hubzoid-backup-20261012-030000.tar.gz"]
    assert all(bk.verify(Path(a["path"]))["ok"] for a in sched.archives(hub))
    assert not sched.due(hub) and sched.due(hub, now=time.time() + 8 * 86400)
    # Earlier archives never go into the next one.
    with tarfile.open(sched.archives(hub)[0]["path"]) as tar:
        assert not any("/backups/" in n for n in tar.getnames())
    monkeypatch.setenv(sched.SETTING, "off")
    assert not sched.due(hub, now=time.time() + 30 * 86400)


def test_a_failed_scheduled_backup_waits_before_trying_again(tmp_path, monkeypatch):
    hub = _standalone(tmp_path)
    monkeypatch.setenv(sched.SETTING, "daily")
    monkeypatch.setattr(sched, "_meta", lambda hub_dir, key: {"at": time.time() - 60, "error": "disk full"}
                        if key == "backup:failed" else None)
    assert not sched.due(hub)
    assert sched.due(hub, now=time.time() + sched.RETRY_AFTER)


def test_the_loop_backs_up_when_due_and_survives_a_failure(tmp_path, monkeypatch):
    hub = _standalone(tmp_path)
    monkeypatch.setenv(sched.SETTING, "weekly")
    calls = []

    def run_once(hub_dir):
        calls.append(hub_dir)
        if len(calls) == 1:
            raise bk.BackupError("disk full")
        raise asyncio.CancelledError

    monkeypatch.setattr(sched, "due", lambda hub_dir: True)
    monkeypatch.setattr(sched, "run_once", run_once)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(sched.loop(hub, first=0, every=0))
    assert len(calls) == 2


def test_rehearsal_restores_into_scratch_and_touches_nothing(tmp_path):
    hub = _standalone(tmp_path)
    archive = tmp_path / "b.tar.gz"
    bk.backup(hub, archive, wait=0)
    before = {p: p.stat().st_mtime_ns for p in hub.rglob("*") if p.is_file()}
    report = bk.rehearse(archive)
    assert report["ok"], report["problems"]
    upgraded = [d for d in report["databases"] if d.get("upgraded")]
    assert upgraded and all(not d["problems"] for d in report["databases"])
    assert {p: p.stat().st_mtime_ns for p in hub.rglob("*") if p.is_file()} == before


def test_the_cli_verifies_lists_and_rehearses(tmp_path, monkeypatch):
    hub = _standalone(tmp_path)
    runner = CliRunner()
    archive = tmp_path / "b.tar.gz"
    r = runner.invoke(cli.app, ["backup", str(hub), "--out", str(archive), "--wait", "0"])
    assert r.exit_code == 0 and "verified" in r.output, r.output
    r = runner.invoke(cli.app, ["backup", "verify", str(archive), "--json"])
    assert r.exit_code == 0 and json.loads(r.output)["ok"]
    r = runner.invoke(cli.app, ["restore", str(archive), "--rehearse", "--json"])
    assert r.exit_code == 0 and json.loads(r.output)["ok"], r.output
    r = runner.invoke(cli.app, ["backup", "list", str(hub), "--json"])
    assert r.exit_code == 0 and json.loads(r.output)["archives"] == []
    cut = tmp_path / "cut.tar.gz"
    cut.write_bytes(archive.read_bytes()[:200])
    assert runner.invoke(cli.app, ["backup", "verify", str(cut)]).exit_code == 1


@pytest.mark.slow  # starts PostgreSQL
def test_postgres_is_dumped_verified_rehearsed_and_restored(tmp_path, monkeypatch, postgres_url):
    from sqlalchemy import create_engine, text

    from hubzoid import access, migrations

    scratch_db = f"hz_scratch_{int(time.time() * 1000)}"
    admin = create_engine(postgres_url, isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        c.execute(text(f"CREATE DATABASE {scratch_db}"))
    admin.dispose()
    scratch = postgres_url.rsplit("/", 1)[0] + "/" + scratch_db

    hub = _standalone(tmp_path)
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", postgres_url)
    access._stores.clear()
    migrations._done.clear()
    from hubzoid.access import store_for

    store_for(hub).grant("pg@example.org", "hub", "use_hub", actor="test")
    archive = tmp_path / "pg.tar.gz"
    index = bk.backup(hub, archive, wait=0)
    assert [d["roles"] for d in index["postgres"]] == [["operational"]] and index["verified"]["postgres"] == 1
    assert "hz_test" in index["postgres"][0]["url"] and index["not_included"] == []

    report = bk.rehearse(archive, postgres_url=scratch)
    assert report["ok"], report["problems"]
    assert report["postgres"][0]["upgraded"]

    engine = create_engine(postgres_url)
    with engine.begin() as c:
        c.execute(text("DELETE FROM hz_grants WHERE subject='pg@example.org'"))
    bk.restore(archive, [(str(tmp_path / "live"), str(tmp_path / "moved"))], postgres_url=postgres_url)
    with engine.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM hz_grants WHERE subject='pg@example.org'")).scalar_one() == 1
    engine.dispose()
