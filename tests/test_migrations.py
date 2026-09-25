"""Versioned schema for Hubzoid's own tables (hubzoid.migrations).

Existing installs created tables lazily, so an upgrade must accept any subset of
today's tables, keep their rows, refuse tables that don't match, refuse a schema
from a newer Hubzoid, and survive many bridges starting at once.
"""
from __future__ import annotations

import subprocess
import sys

import pytest
from sqlalchemy import create_engine, inspect, text

from hubzoid import migrations
from hubzoid.migrations import SchemaError

OPERATIONAL = {"hz_grants", "hz_policy_revision", "hz_identities", "hz_identity_attrs",
               "hz_meta", "hz_access_audit", "hz_workflows", "hz_workflow_kv", "hz_usage",
               "hz_access_decisions"}


@pytest.fixture(autouse=True)
def _fresh_cache():
    migrations._done.clear()
    yield
    migrations._done.clear()


def _sqlite(tmp_path, name="ops.db"):
    return create_engine(f"sqlite:///{tmp_path / name}")


def _tables(engine):
    return set(inspect(engine).get_table_names())


def test_fresh_database_gets_every_table_at_head(tmp_path):
    eng = _sqlite(tmp_path)
    migrations.upgrade(eng, "operational")
    migrations.upgrade(eng, "hub")
    assert OPERATIONAL | {"hz_inbound_history"} <= _tables(eng)
    assert migrations.current(eng, "operational") == migrations.head("operational")
    assert migrations.current(eng, "hub") == migrations.head("hub")
    with eng.connect() as c:
        assert c.execute(text("SELECT rev FROM hz_policy_revision WHERE id=1")).scalar() == 0


def test_existing_partial_install_keeps_rows_and_gains_missing_tables(tmp_path):
    eng = _sqlite(tmp_path)
    with eng.begin() as c:  # what an older Hubzoid created lazily: just two tables
        c.execute(text("CREATE TABLE hz_grants (subject TEXT NOT NULL, hub TEXT NOT NULL, "
                       "permission TEXT NOT NULL, PRIMARY KEY (subject, hub, permission))"))
        c.execute(text("CREATE TABLE hz_meta (k TEXT PRIMARY KEY, v TEXT)"))
        c.execute(text("INSERT INTO hz_grants VALUES ('a@x.org', 'sales', 'use_hub')"))
        c.execute(text("INSERT INTO hz_meta VALUES ('casbin_authoritative:sales', '1')"))
    migrations.upgrade(eng, "operational")
    assert OPERATIONAL <= _tables(eng)
    with eng.connect() as c:
        assert c.execute(text("SELECT count(*) FROM hz_grants")).scalar() == 1
        assert c.execute(text("SELECT v FROM hz_meta")).scalar() == "1"
    assert migrations.current(eng, "operational") == migrations.head("operational")


def test_table_that_does_not_match_is_refused(tmp_path):
    eng = _sqlite(tmp_path)
    with eng.begin() as c:
        c.execute(text("CREATE TABLE hz_grants (subject TEXT, hub TEXT)"))  # no permission column
    with pytest.raises(SchemaError, match="hz_grants"):
        migrations.upgrade(eng, "operational")
    assert migrations.current(eng, "operational") is None  # nothing stamped


def test_schema_from_a_newer_hubzoid_is_refused(tmp_path):
    eng = _sqlite(tmp_path)
    migrations.upgrade(eng, "operational")
    migrations._done.clear()
    with eng.begin() as c:
        c.execute(text("UPDATE hz_alembic_operational SET version_num='op_9999'"))
    with pytest.raises(SchemaError, match="newer"):
        migrations.upgrade(eng, "operational")


def test_both_stores_share_one_standalone_file(tmp_path):
    eng = _sqlite(tmp_path, "hub.db")
    migrations.upgrade(eng, "operational")
    migrations.upgrade(eng, "hub")
    assert {"hz_alembic_operational", "hz_alembic_hub"} <= _tables(eng)


_RACE = """
import sys
from sqlalchemy import create_engine
from hubzoid import migrations
eng = create_engine(f"sqlite:///{sys.argv[1]}")
migrations.upgrade(eng, "operational")
migrations.upgrade(eng, "hub")
print("OK")
"""


def test_many_bridges_starting_at_once(tmp_path):
    db = tmp_path / "shared.db"
    procs = [subprocess.Popen([sys.executable, "-c", _RACE, str(db)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for _ in range(8)]
    outs = [p.communicate(timeout=120) for p in procs]
    assert all(o[0].strip() == "OK" for o in outs), [o[1][-500:] for o in outs]
    eng = create_engine(f"sqlite:///{db}")
    with eng.connect() as c:
        assert c.execute(text("SELECT count(*) FROM hz_alembic_operational")).scalar() == 1
    assert migrations.current(eng, "operational") == migrations.head("operational")


def _reset_pg(url):
    eng = create_engine(url)
    with eng.begin() as c:
        for t in sorted(OPERATIONAL) + ["hz_alembic_operational", "hz_inbound_history", "hz_alembic_hub"]:
            c.execute(text(f"DROP TABLE IF EXISTS {t} CASCADE"))
    eng.dispose()


def test_postgres_fresh_and_partial_upgrade(postgres_url):
    _reset_pg(postgres_url)
    eng = create_engine(postgres_url)
    with eng.begin() as c:  # an early Postgres install: REAL time column, one table
        c.execute(text("CREATE TABLE hz_access_audit (ts REAL NOT NULL, actor TEXT, action TEXT NOT NULL, "
                       "subject TEXT, hub TEXT, permission TEXT)"))
        c.execute(text("INSERT INTO hz_access_audit (ts, action) VALUES (1.5, 'grant')"))
    migrations.upgrade(eng, "operational")
    migrations.upgrade(eng, "hub")
    assert OPERATIONAL | {"hz_inbound_history"} <= _tables(eng)
    with eng.connect() as c:
        assert c.execute(text("SELECT count(*) FROM hz_access_audit")).scalar() == 1
        kind = c.execute(text("SELECT data_type FROM information_schema.columns "
                              "WHERE table_name='hz_access_audit' AND column_name='ts'")).scalar()
    assert kind == "double precision"
    eng.dispose()


_PG_RACE = """
import sys
from sqlalchemy import create_engine
from hubzoid import migrations
migrations.upgrade(create_engine(sys.argv[1]), "operational")
print("OK")
"""


def test_postgres_many_bridges_starting_at_once(postgres_url):
    _reset_pg(postgres_url)
    procs = [subprocess.Popen([sys.executable, "-c", _PG_RACE, postgres_url],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for _ in range(8)]
    outs = [p.communicate(timeout=120) for p in procs]
    assert all(o[0].strip() == "OK" for o in outs), [o[1][-500:] for o in outs]
    eng = create_engine(postgres_url)
    assert migrations.current(eng, "operational") == migrations.head("operational")
    eng.dispose()
