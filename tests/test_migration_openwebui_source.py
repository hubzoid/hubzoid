"""Reading an Open WebUI (1.x) database for the move to the web app: read-only,
the deployment's registered database first, and never a stale fallback."""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from hubzoid import deployment
from hubzoid.migration.openwebui import source


def _db(path, email):
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as con:
        con.execute(text('CREATE TABLE "user" (id TEXT PRIMARY KEY, email TEXT, role TEXT)'))
        con.execute(text('INSERT INTO "user" VALUES (\'u1\', :email, \'user\')'), {"email": email})
    engine.dispose()
    return path


def _email(hub):
    con = source.connect_ro(hub)
    if con is None:
        return None
    with con:
        return con.execute(text('SELECT email FROM "user"')).scalar()


@pytest.fixture
def hub(tmp_path, monkeypatch):
    for key in ("HUBZOID_DEPLOYMENT", "DATABASE_URL", "DATABASE_SCHEMA", "HUBZOID_OWUI_DB"):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


def test_reads_are_read_only(hub, monkeypatch):
    monkeypatch.setenv("HUBZOID_OWUI_DB", str(_db(hub / "webui.db", "alice@example.com")))
    con = source.connect_ro(hub)
    with con:
        with pytest.raises(DBAPIError):
            con.execute(text('DELETE FROM "user"'))
    assert _email(hub) == "alice@example.com"


def test_registered_database_wins_and_a_conflicting_url_is_refused(hub, monkeypatch):
    real = _db(hub / "real.db", "alice@example.com")
    stale = _db(hub / "stale.db", "stale@example.com")
    deployment.save(hub / "gateway.json", hubs=[{"key": "test", "path": str(hub)}],
                    operational_url=f"sqlite:///{hub / 'operational.db'}",
                    owui_db=str(stale), owui_database_url=f"sqlite:///{real}")
    assert _email(hub) == "alice@example.com"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{stale}")
    assert _email(hub) is None  # the environment disagrees with the record: nothing is read


def test_an_unavailable_database_never_falls_back_or_logs_its_url(hub, monkeypatch, caplog):
    monkeypatch.setenv("HUBZOID_OWUI_DB", str(_db(hub / "stale.db", "stale@example.com")))
    monkeypatch.setenv("DATABASE_URL",
                       "postgresql+psycopg://synthetic:secret-canary@127.0.0.1:1/unavailable")
    assert _email(hub) is None
    assert "secret-canary" not in caplog.text
