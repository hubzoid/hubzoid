"""Regressions for the platform review of the web app release.

1. A plain ``postgresql://`` DATABASE_URL means psycopg 3, which Hubzoid
   installs, everywhere Hubzoid opens it: the upgrade guard, `hubzoid doctor`
   and `hubzoid backup`. A store that cannot be read is never taken for one
   with no accounts.
3. Backups keep files people attached whose names look like credential or
   temporary files. Only the real credential and temporary files stay out.
"""
from __future__ import annotations

import io
import sqlite3
import sys
import tarfile
import uuid
from pathlib import Path

import pytest
import typer
from rich.console import Console

from hubzoid import backup as bk
from hubzoid import cli, upgrade
from hubzoid import doctor as doc

_ENV = ("HUBZOID_OPERATIONAL_DB", "HUBZOID_DBOS_DB", "DATABASE_URL", "DATABASE_SCHEMA",
        "HUBZOID_DEPLOYMENT", "HUBZOID_OWUI_DB", "HUBZOID_UI", "HUBZOID_AUTH", "WEBUI_AUTH",
        "HUBZOID_ADMIN_EMAIL", "WEBUI_ADMIN_EMAIL", "HUBZOID_HOST", "HUBZOID_SECRET_KEY", "MODEL")


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)
    # The core install has psycopg (3) and not psycopg2, which only the legacy
    # Open WebUI extra brought in. Make that so here whatever is installed.
    monkeypatch.setitem(sys.modules, "psycopg2", None)
    from hubzoid import access, db, migrations, secretbox

    access._stores.clear()
    migrations._done.clear()
    secretbox.reset_cache()
    yield
    access._stores.clear()
    for eng in db._engines.values():
        eng.dispose()
    db._engines.clear()
    secretbox.reset_cache()


@pytest.fixture
def plain_pg(postgres_url):
    """A database of its own on the test PostgreSQL server, as the plain
    ``postgresql://`` URL a 1.0.x deployment has in DATABASE_URL."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    name = f"hz_platform_{uuid.uuid4().hex[:8]}"
    admin = create_engine(postgres_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            c.execute(text(f'CREATE DATABASE "{name}"'))
        url = make_url(postgres_url).set(drivername="postgresql", database=name)
        yield url.render_as_string(hide_password=False)
        with admin.connect() as c:
            c.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    finally:
        admin.dispose()


def _pg(url: str, *statements: str) -> None:
    from sqlalchemy import create_engine, text

    engine = create_engine(url.replace("postgresql://", "postgresql+psycopg://", 1))
    try:
        with engine.begin() as c:
            for s in statements:
                c.execute(text(s))
    finally:
        engine.dispose()


def _pg_rows(url: str, sql: str) -> list:
    from sqlalchemy import create_engine, text

    engine = create_engine(url.replace("postgresql://", "postgresql+psycopg://", 1))
    try:
        with engine.connect() as c:
            return [tuple(r) for r in c.execute(text(sql))]
    finally:
        engine.dispose()


def _hub(root: Path, name: str = "hub") -> Path:
    hub = root / name
    hub.mkdir(parents=True)
    (hub / "AGENTS.md").write_text(
        f"---\nname: {name}\ndescription: d\nmodel: openrouter/anthropic/claude-haiku-4.5\n---\nbody")
    return hub


def _owui_sqlite(hub: Path, people: int = 2) -> None:
    path = hub / ".openwebui-data" / "webui.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as con:
        con.execute('CREATE TABLE "user" (id TEXT PRIMARY KEY, email TEXT)')
        for i in range(people):
            con.execute('INSERT INTO "user" VALUES (?, ?)', (f"u{i}", f"p{i}@example.com"))


@pytest.fixture
def said(monkeypatch):
    """What the CLI prints, unwrapped."""
    out = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=out, width=400, color_system=None))
    return lambda: " ".join(out.getvalue().split())


# ---------------------------------------------------------------------------
# 1. Plain postgresql:// URLs, and an unreadable store is not an empty one
# ---------------------------------------------------------------------------
def test_upgrade_guard_reads_accounts_through_a_plain_postgresql_url(plain_pg, tmp_path, monkeypatch):
    hub = _hub(tmp_path)
    _pg(plain_pg, "CREATE TABLE hz_users (id TEXT PRIMARY KEY, email TEXT)",
        "INSERT INTO hz_users VALUES ('o1', 'admin@localhost'), ('p1', 'person@example.com')")
    monkeypatch.setenv("DATABASE_URL", plain_pg)
    assert upgrade.hubzoid_accounts(hub) == 1


def test_a_migrated_postgresql_deployment_is_not_stopped(plain_pg, tmp_path, monkeypatch, said):
    """1.0.x kept Open WebUI's tables and Hubzoid's in the DATABASE_URL
    database. After the move both have people: sign-in on must start."""
    hub = _hub(tmp_path)
    _pg(plain_pg, 'CREATE TABLE "user" (id TEXT PRIMARY KEY, email TEXT)',
        """INSERT INTO "user" VALUES ('u0', 'p0@example.com'), ('u1', 'p1@example.com')""",
        "CREATE TABLE hz_users (id TEXT PRIMARY KEY, email TEXT)",
        "INSERT INTO hz_users VALUES ('u0', 'p0@example.com')")
    monkeypatch.setenv("DATABASE_URL", plain_pg)
    assert upgrade.openwebui_accounts(hub)[1] == 2
    cli._check_openwebui_upgrade(hub, auth_on=True)  # no typer.Exit
    assert "no Hubzoid accounts" not in said()
    monkeypatch.setenv("HUBZOID_AUTH", "true")
    assert doc._openwebui_data(hub, auth_on=True) is None


def test_doctor_reads_a_plain_postgresql_store(plain_pg, tmp_path, monkeypatch):
    hub = _hub(tmp_path)
    _pg(plain_pg, "CREATE TABLE hz_meta (k TEXT PRIMARY KEY, v TEXT)")
    monkeypatch.setenv("DATABASE_URL", plain_pg)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    checks = {c.id: c for c in doc.run(hub, fetch_secrets=False)}
    assert checks["db.operational"].status != "fail", checks["db.operational"].summary
    assert "db.read" not in checks
    assert checks["backup.age"].summary.startswith("No backup recorded")
    assert not any("psycopg2" in c.summary for c in checks.values())


def test_backup_of_a_plain_postgresql_deployment(plain_pg, tmp_path, monkeypatch):
    hub = _hub(tmp_path)
    (hub / "output").mkdir()
    (hub / "output" / "report.txt").write_text("report")
    _pg(plain_pg, "CREATE TABLE hz_meta (k TEXT PRIMARY KEY, v TEXT)")
    monkeypatch.setenv("DATABASE_URL", plain_pg)
    monkeypatch.setattr(bk, "running_runs", lambda plan, unknown=None: [])
    index = bk.backup(hub, tmp_path / "b.tar.gz", wait=0)
    assert (tmp_path / "b.tar.gz").is_file() and index["not_included"]
    assert [k for (k,) in _pg_rows(plain_pg, "SELECT k FROM hz_meta")] == ["backup:last"]


def _garbage_store(hub: Path) -> None:
    (hub / ".hubzoid").mkdir(parents=True, exist_ok=True)
    (hub / ".hubzoid" / "hub.db").write_bytes(b"not a database, " * 64)


def test_an_unreadable_store_is_not_an_empty_one(tmp_path):
    hub = _hub(tmp_path)
    assert upgrade.hubzoid_accounts(hub) == 0  # before the first start
    _garbage_store(hub)
    with pytest.raises(upgrade.AccountsUnreadable) as caught:
        upgrade.hubzoid_accounts(hub)
    assert str(caught.value) == "DatabaseError"  # the error type, never a URL


def test_the_upgrade_guard_stops_with_the_reason_when_accounts_cannot_be_read(tmp_path, said):
    hub = _hub(tmp_path)
    _owui_sqlite(hub)
    _garbage_store(hub)
    with pytest.raises(typer.Exit) as stopped:
        cli._check_openwebui_upgrade(hub, auth_on=True)
    assert stopped.value.exit_code == 1
    out = said()
    assert "Hubzoid accounts could not be read (DatabaseError)" in out
    assert "hubzoid doctor" in out
    assert "no Hubzoid accounts yet" not in out and "Move accounts" not in out


def test_local_mode_starts_when_accounts_cannot_be_read(tmp_path, said):
    hub = _hub(tmp_path)
    _owui_sqlite(hub)
    _garbage_store(hub)
    cli._check_openwebui_upgrade(hub, auth_on=False)  # nothing to lock out
    assert "could not be read (DatabaseError)" in said()


def test_doctor_reports_unreadable_accounts(tmp_path, monkeypatch):
    hub = _hub(tmp_path)
    _owui_sqlite(hub)
    _garbage_store(hub)
    found = doc._openwebui_data(hub, auth_on=True)
    assert found.status == "fail" and "could not be read (DatabaseError)" in found.summary
    assert "Hubzoid has none" not in found.summary
    assert doc._openwebui_data(hub, auth_on=False).status == "warn"
    monkeypatch.setenv("HUBZOID_AUTH", "true")
    signin = doc._web_app_signin(hub)
    assert signin.status == "warn" and "could not be read (DatabaseError)" in signin.summary
    assert "no Hubzoid account yet" not in signin.summary


# ---------------------------------------------------------------------------
# 3. Attachments named like credential or temporary files are backed up
# ---------------------------------------------------------------------------
_LOOKALIKES = ("secret.key", "artifact_secret", "report.tmp", "draft.part", "notes-journal",
               "data-wal", "data-shm")


def _rels(archive: Path) -> set[str]:
    with tarfile.open(archive) as t:
        return {n.split("/", 1)[1] for n in t.getnames() if "/" in n}


def _deployment_with_lookalikes(tmp_path: Path) -> Path:
    from hubzoid import _signing, secretbox

    hub = _hub(tmp_path / "live")
    uploads = hub / ".hubzoid" / "chats" / "c1" / "uploads"
    uploads.mkdir(parents=True)
    for name in _LOOKALIKES:
        (uploads / name).write_text(f"attached {name}")
    (hub / "output" / "s1").mkdir(parents=True)
    (hub / "output" / "s1" / "export.tmp").write_text("agent output")
    owui = hub / ".openwebui-data" / "uploads"
    owui.mkdir(parents=True)
    (owui / "f1_report.tmp").write_text("attached in Open WebUI")
    # The real credential and temporary files.
    _signing._secret(hub)  # .hubzoid/artifact_secret
    secretbox.keys(hub)    # .hubzoid/secret.key
    (hub / ".hubzoid" / "deployment.123.tmp").write_text("{}")
    (hub / "output" / "s1" / ".export.pdf.123.tmp").write_text("half an artifact")
    return hub


def test_backup_keeps_attachments_named_like_secrets_or_temporary_files(tmp_path):
    hub = _deployment_with_lookalikes(tmp_path)
    live = sqlite3.connect(hub / ".hubzoid" / "extra.db")  # a live WAL database
    live.execute("PRAGMA journal_mode=WAL")
    live.execute("CREATE TABLE t (x)")
    live.execute("INSERT INTO t VALUES (1)")
    live.commit()
    try:
        assert (hub / ".hubzoid" / "extra.db-wal").exists()
        bk.backup(hub, tmp_path / "b.tar.gz", wait=0)
    finally:
        live.close()
    rels = _rels(tmp_path / "b.tar.gz")
    for name in _LOOKALIKES:
        assert f"chats/c1/uploads/{name}" in rels, name
    assert "s1/export.tmp" in rels and "uploads/f1_report.tmp" in rels
    # Still left out: credentials, Hubzoid's temporary files, SQLite sidecars.
    assert {"secret.key", "artifact_secret", "deployment.123.tmp", "s1/.export.pdf.123.tmp",
            "extra.db-wal", "extra.db-shm"}.isdisjoint(rels)
    assert "extra.db" in rels

    new = tmp_path / "elsewhere"
    bk.restore(tmp_path / "b.tar.gz", [(str(tmp_path / "live"), str(new))])
    uploads = new / "hub" / ".hubzoid" / "chats" / "c1" / "uploads"
    for name in _LOOKALIKES:
        assert (uploads / name).read_text() == f"attached {name}"


def test_secrets_mode_adds_credentials_and_still_leaves_temporary_files_out(tmp_path):
    hub = _deployment_with_lookalikes(tmp_path)
    bk.backup(hub, tmp_path / "s.tar.gz", wait=0, include_secrets=True)
    rels = _rels(tmp_path / "s.tar.gz")
    assert {"secret.key", "artifact_secret", "chats/c1/uploads/report.tmp"} <= rels
    assert "deployment.123.tmp" not in rels


def test_restore_carries_over_only_the_real_credential_files(tmp_path):
    hub = _deployment_with_lookalikes(tmp_path)
    key = (hub / ".hubzoid" / "secret.key").read_bytes()
    bk.backup(hub, tmp_path / "b.tar.gz", wait=0)
    later = hub / ".hubzoid" / "chats" / "c2" / "uploads"  # attached after the backup
    later.mkdir(parents=True)
    (later / "secret.key").write_text("attached later")
    bk.restore(tmp_path / "b.tar.gz")  # in place
    assert (hub / ".hubzoid" / "secret.key").read_bytes() == key
    assert not (hub / ".hubzoid" / "chats" / "c2").exists()


def test_an_archive_written_inside_a_saved_folder_leaves_itself_out(tmp_path):
    hub = _hub(tmp_path)
    (hub / "output").mkdir()
    (hub / "output" / "report.txt").write_text("report")
    bk.backup(hub, hub / "output" / "b.tar.gz", wait=0)
    rels = _rels(hub / "output" / "b.tar.gz")
    assert "report.txt" in rels and not any(r.startswith("b.tar.gz") for r in rels)
