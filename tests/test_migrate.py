"""Tests for the access migration (flatten legacy → direct Casbin grants)."""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from hubzoid.access import migrate
from hubzoid.access.migrate import MigrationBlocked
from hubzoid.access.store import EVERYONE, USE_HUB, GrantStore

TEST_HUB = Path(
    "/Users/shreyarao/Desktop/WaveAssist/Hubzoid/HubzoidTestHub/test-hub"
)


@pytest.fixture()
def store(tmp_path):
    return GrantStore(create_engine(f"sqlite:///{tmp_path / 'hub.db'}"))


@pytest.mark.skipif(
    not (TEST_HUB / "identity" / "access.csv").exists(), reason="test hub absent"
)
def test_plan_from_csv_and_apply(store):
    plan = migrate.plan_from_csv(TEST_HUB, "test-hub")
    # the test hub's access.csv: tester@example.com, groups=testers, center=adyar
    assert ("tester@example.com", "test-hub", "testers") in plan.grants
    assert ("test-hub", "tester@example.com", "center", "adyar") in plan.attrs

    migrate.apply(store, plan, authoritative=True)
    assert store.can("tester@example.com", "test-hub", "testers")
    assert store.can("tester@example.com", "test-hub", USE_HUB)   # implied
    assert store.get_attr("test-hub", "tester@example.com", "center") == "adyar"
    assert store.is_authoritative() is True

    # cutover gate: a full static diff is zero after apply
    d = migrate.diff(store, plan)
    assert d["missing"] == [] and d["extra"] == []

    # an identity row was recorded for the migrated email
    assert store.identity("tester@example.com")["email"] == "tester@example.com"


def test_preflight_blocks_function_roster(tmp_path):
    ident = tmp_path / "identity"
    ident.mkdir()
    (ident / "access.py").write_text(          # a function-backed roster
        "def resolve(surface, handle):\n    return None\n"
        "def groups_for_email(email):\n    return ['live_group']\n"
    )
    with pytest.raises(MigrationBlocked):
        migrate.preflight(tmp_path)


def _owui_fixture(tmp_path):
    """A tiny Open-WebUI-0.11-shaped DB: user, group, model."""
    eng = create_engine(f"sqlite:///{tmp_path / 'owui.db'}")
    with eng.begin() as c:
        c.execute(text("CREATE TABLE user (id TEXT, email TEXT)"))
        c.execute(text('CREATE TABLE "group" (id TEXT, name TEXT, user_ids TEXT)'))
        c.execute(text("CREATE TABLE model (id TEXT, access_control TEXT)"))
        c.execute(text("INSERT INTO user VALUES ('u1','alice@corp'), ('u2','bob@corp')"))
        c.execute(text('INSERT INTO "group" VALUES (\'g1\',\'coord\',\'["u1","u2"]\')'))
    return eng


def test_plan_from_owui_group_grant(store, tmp_path):
    eng = _owui_fixture(tmp_path)
    with eng.begin() as c:
        # a model granted to group g1 (read)
        c.execute(text(
            "INSERT INTO model VALUES ('m1', '{\"read\": {\"group_ids\": [\"g1\"], \"user_ids\": []}}')"
        ))
    plan = migrate.plan_from_owui(eng, "opshub", model_id="m1")
    migrate.apply(store, plan, authoritative=True)
    assert store.can("alice@corp", "opshub", USE_HUB)
    assert store.can("bob@corp", "opshub", USE_HUB)


def test_plan_from_owui_public_model_wildcard(store, tmp_path):
    eng = _owui_fixture(tmp_path)
    with eng.begin() as c:
        c.execute(text("INSERT INTO model VALUES ('m1', NULL)"))  # public
    plan = migrate.plan_from_owui(eng, "publichub", model_id="m1")
    assert (EVERYONE, "publichub", USE_HUB) in plan.grants
    migrate.apply(store, plan, authoritative=True)
    assert store.can("anyone-signed-in", "publichub", USE_HUB)


def test_plan_from_owui_refuses_unknown_schema(store, tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'weird.db'}")
    with eng.begin() as c:
        c.execute(text("CREATE TABLE something_else (x TEXT)"))
    with pytest.raises(MigrationBlocked):
        migrate.plan_from_owui(eng, "hub")
