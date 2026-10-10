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

OPERATIONAL = {"hz_mcp_oauth", "hz_grants", "hz_policy_revision", "hz_identities", "hz_identity_attrs",
               "hz_meta", "hz_access_audit", "hz_workflows", "hz_workflow_kv", "hz_usage",
               "hz_access_decisions", "hz_change_requests", "hz_connect_states",
               "hz_artifacts", "hz_artifact_shares", "hz_artifact_links", "hz_email_deliveries",
               # web app (op_0009 to op_0012): accounts, conversations, connections
               "hz_users", "hz_user_identities", "hz_sessions", "hz_auth_links", "hz_auth_attempts",
               "hz_conversations", "hz_messages", "hz_shares",
               "hz_connectors", "hz_connector_tokens", "hz_connector_flows", "hz_connector_agents",
               "hz_workflow_owner", "hz_workflow_events", "hz_workflow_alerts"}


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


@pytest.mark.slow
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


@pytest.mark.slow
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


def test_access_audit_gains_surface_and_request_id(tmp_path):
    """op_0004 adds nullable audit columns without touching existing rows."""
    from sqlalchemy import create_engine, inspect

    eng = create_engine(f"sqlite:///{tmp_path / 'op.db'}")
    migrations.upgrade(eng, "operational")
    cols = {c["name"] for c in inspect(eng).get_columns("hz_access_audit")}
    assert {"surface", "request_id"} <= cols


def test_workflow_state_keeps_its_rows_when_owner_joins_the_key(tmp_path):
    """op_0005 rebuilds hz_workflow_kv with an owner column; rows written before
    stay, with owner '' (kept, assigned to no person)."""
    from alembic.runtime.environment import EnvironmentContext

    eng = _sqlite(tmp_path)
    cfg, script = migrations._script("operational")

    def to_0004(rev, context):
        return script._upgrade_revs("op_0004", rev)

    with EnvironmentContext(cfg, script, fn=to_0004, destination_rev="op_0004") as env:
        with eng.connect() as conn:
            env.configure(connection=conn, version_table=migrations.STORES["operational"])
            with env.begin_transaction():
                env.run_migrations()
            conn.commit()
    with eng.begin() as c:
        c.execute(text("INSERT INTO hz_workflow_kv (hub, workflow, k, v) "
                       "VALUES ('sales', 'digest', 'cursor', '41')"))
    migrations.upgrade(eng, "operational")
    with eng.connect() as c:
        rows = c.execute(text("SELECT hub, workflow, owner, k, v FROM hz_workflow_kv")).fetchall()
    assert [tuple(r) for r in rows] == [("sales", "digest", "", "cursor", "41")]
    pk = inspect(eng).get_pk_constraint("hz_workflow_kv")["constrained_columns"]
    assert pk == ["hub", "workflow", "owner", "k"]


def test_groups_are_dropped_and_their_access_moves_to_the_members(tmp_path):
    """op_0015: each group grant becomes the same grant for each member who is
    not blocked, artifacts shared with a group by name are shared with those
    members, an unknown group share goes, and the group tables are dropped."""
    from alembic.runtime.environment import EnvironmentContext

    eng = _sqlite(tmp_path)
    cfg, script = migrations._script("operational")

    def to_0014(rev, context):
        return script._upgrade_revs("op_0014", rev)

    with EnvironmentContext(cfg, script, fn=to_0014, destination_rev="op_0014") as env:
        with eng.connect() as conn:
            env.configure(connection=conn, version_table=migrations.STORES["operational"])
            with env.begin_transaction():
                env.run_migrations()
            conn.commit()
    with eng.begin() as c:
        c.execute(text("INSERT INTO hz_groups (id, name, created_at, updated_at) "
                       "VALUES ('g_fin', 'Finance Team', 1, 1)"))
        for email in ("ann@x.org", "bob@x.org", "eve@x.org"):
            c.execute(text("INSERT INTO hz_group_members (group_id, email, added_at) "
                           "VALUES ('g_fin', :e, 1)"), {"e": email})
        c.execute(text("INSERT INTO hz_meta (k, v) VALUES ('suspended:eve@x.org', '1')"))
        c.execute(text("INSERT INTO hz_grants (subject, hub, permission) VALUES "
                       "('group:g_fin', 'sales', 'ledger'), ('group:g_fin', 'sales', 'use_hub'), "
                       "('bob@x.org', 'sales', 'use_hub')"))
        c.execute(text("INSERT INTO hz_artifact_shares (artifact_id, kind, principal, added) VALUES "
                       "('a1', 'group', 'finance team', 1), ('a2', 'group', 'owui-only', 1)"))
    migrations.upgrade(eng, "operational")
    with eng.connect() as c:
        grants = set(c.execute(text("SELECT subject, hub, permission FROM hz_grants")).fetchall())
        shares = set(c.execute(text("SELECT artifact_id, kind, principal FROM hz_artifact_shares")))
    assert grants == {("ann@x.org", "sales", "ledger"), ("ann@x.org", "sales", "use_hub"),
                      ("bob@x.org", "sales", "ledger"), ("bob@x.org", "sales", "use_hub")}
    assert shares == {("a1", "user", "ann@x.org"), ("a1", "user", "bob@x.org")}
    assert not {"hz_groups", "hz_group_members"} & _tables(eng)



def test_existing_connectors_stay_offered_in_every_known_agent(tmp_path):
    """op_0016: a connector registered before agents offered connectors is
    offered in every agent the store knows, so nothing changes on upgrade."""
    from alembic.runtime.environment import EnvironmentContext

    eng = _sqlite(tmp_path)
    cfg, script = migrations._script("operational")

    def to_0015(rev, context):
        return script._upgrade_revs("op_0015", rev)

    with EnvironmentContext(cfg, script, fn=to_0015, destination_rev="op_0015") as env:
        with eng.connect() as conn:
            env.configure(connection=conn, version_table=migrations.STORES["operational"])
            with env.begin_transaction():
                env.run_migrations()
            conn.commit()
    with eng.begin() as c:
        c.execute(text("INSERT INTO hz_connectors (id, name, url, created_at, updated_at) "
                       "VALUES ('gmail', 'Gmail', 'https://g.example/mcp', 1, 1)"))
        c.execute(text("INSERT INTO hz_grants (subject, hub, permission) VALUES "
                       "('a@x.org', 'sales', 'use_hub'), ('b@x.org', 'ops', 'use_hub'), "
                       "('root@x.org', '*', 'manage_access')"))
    migrations.upgrade(eng, "operational")
    with eng.connect() as c:
        offers = set(c.execute(text("SELECT connector_id, hub FROM hz_connector_agents")))
    assert offers == {("gmail", "sales"), ("gmail", "ops")}


def _previous_phone_schema(engine):
    migrations.upgrade(engine, "operational")
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX hz_identities_phone_unique"))
        # Undo op_0018 too, so the upgrade from op_0016 runs it again cleanly.
        for table, column in (("hz_connect_states", "account"), ("hz_connectors", "shared_header"),
                              ("hz_connectors", "shared_secret_enc")):
            conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
        conn.execute(text("UPDATE hz_alembic_operational SET version_num='op_0016'"))
    migrations._done.clear()


def test_phone_upgrade_normalizes_and_allows_unassigned_people(tmp_path):
    eng = _sqlite(tmp_path)
    _previous_phone_schema(eng)
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO hz_identities (subject,phone,pending,created) VALUES "
                          "('one@example.org','+1 555 000 1111',0,0),"
                          "('two@example.org','',0,0),('three@example.org',NULL,0,0)"))
    migrations.upgrade(eng, "operational")
    with eng.connect() as conn:
        assert dict(conn.execute(text("SELECT subject,phone FROM hz_identities")).all()) == {
            'one@example.org': '15550001111', 'two@example.org': None, 'three@example.org': None}
    assert migrations.current(eng, 'operational') == migrations.head('operational')


@pytest.mark.parametrize('other,problem', [('15550001111', 'more than one'), ('123', 'invalid')])
def test_ambiguous_phone_upgrade_stops_without_changing_assignments(tmp_path, other, problem):
    eng = _sqlite(tmp_path)
    _previous_phone_schema(eng)
    with eng.begin() as conn:
        conn.execute(text("INSERT INTO hz_identities (subject,phone,pending,created) VALUES "
                          "('one@example.org','+1 555 000 1111',0,0),"
                          "('two@example.org',:p,0,0)"), {'p': other})
    with pytest.raises(SchemaError, match=problem):
        migrations.upgrade(eng, "operational")
    with eng.connect() as conn:
        assert conn.execute(text("SELECT phone FROM hz_identities WHERE subject='one@example.org'")).scalar() == '+1 555 000 1111'
    assert migrations.current(eng, 'operational') == 'op_0016'
    with eng.begin() as conn:
        conn.execute(text("UPDATE hz_identities SET phone=NULL WHERE subject='two@example.org'"))
    migrations.upgrade(eng, "operational")
    assert migrations.current(eng, 'operational') == migrations.head('operational')
