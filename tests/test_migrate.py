"""Tests for the access migration (flatten legacy → direct Casbin grants)."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from hubzoid.access import migrate
from hubzoid.access.migrate import MigrationBlocked
from hubzoid.access.store import EVERYONE, USE_HUB, GrantStore

TEST_HUB = Path(__file__).resolve().parents[2] / "HubzoidTestHub" / "test-hub"


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
    assert store.can("tester@example.com", "test-hub", USE_HUB)  # implied
    assert store.get_attr("test-hub", "tester@example.com", "center") == "adyar"
    # per-hub authority marker (not deployment-global)
    assert store.is_authoritative("test-hub") is True
    assert store.is_authoritative("some-other-hub") is False

    # cutover gate: a full static diff is zero after apply
    d = migrate.diff(store, plan)
    assert d["missing"] == [] and d["extra"] == []

    # an identity row was recorded for the migrated email
    assert store.identity("tester@example.com")["email"] == "tester@example.com"


def test_preflight_blocks_function_roster(tmp_path):
    ident = tmp_path / "identity"
    ident.mkdir()
    (ident / "access.py").write_text(  # a function-backed roster
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
        c.execute(
            text("INSERT INTO user VALUES ('u1','alice@corp'), ('u2','bob@corp')")
        )
        c.execute(text("INSERT INTO \"group\" VALUES ('g1','coord','[\"u1\",\"u2\"]')"))
    return eng


def test_plan_from_owui_group_grant(store, tmp_path):
    eng = _owui_fixture(tmp_path)
    with eng.begin() as c:
        # a model granted to group g1 (read)
        c.execute(
            text(
                'INSERT INTO model VALUES (\'m1\', \'{"read": {"group_ids": ["g1"], "user_ids": []}}\')'
            )
        )
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


def test_plan_from_owui_unknown_model_not_public(store, tmp_path):
    eng = _owui_fixture(tmp_path)
    with eng.begin() as c:
        c.execute(text("INSERT INTO model VALUES ('m1', NULL)"))
    # asking for a model that isn't there must NOT become a public wildcard grant
    with pytest.raises(MigrationBlocked):
        migrate.plan_from_owui(eng, "hub", model_id="does-not-exist")


def test_apply_refuses_empty_cutover(store):
    empty = migrate.MigrationPlan()
    with pytest.raises(MigrationBlocked):
        migrate.apply(store, empty, authoritative=True)  # would lock everyone out
    assert store.is_authoritative() is False  # marker untouched


def test_apply_refuses_conflicted_cutover(store):
    plan = migrate.MigrationPlan()
    plan.add_grant("a@x", "hub", "prod_in")
    plan.conflicts.append("duplicate email")
    with pytest.raises(MigrationBlocked):
        migrate.apply(store, plan, authoritative=True)


def test_standalone_csv_baseline_uses_legacy_resolver(tmp_path):
    ident = tmp_path / "identity"
    ident.mkdir()
    (ident / "access.csv").write_text(
        "\ufeff Email , Groups , Phone \nAlice@example.com,ledger,+919876543210\nalice@example.com,inventory,+919876543211\n"
    )
    plan = migrate.plan_standalone_public(tmp_path, migrate.plan_from_csv(tmp_path))
    assert not plan.conflicts
    assert (
        "alice@example.com",
        tmp_path.name.lower(),
        "inventory",
        True,
    ) in plan.expected
    assert (
        "__future_signed_in__",
        tmp_path.name.lower(),
        "use_hub",
        True,
    ) in plan.expected
    assert not migrate.verify_effective(plan)


def test_cli_refuses_unverified_csv_cutover(tmp_path):
    from typer.testing import CliRunner
    from hubzoid.cli import app

    ident = tmp_path / "identity"
    ident.mkdir()
    (ident / "access.csv").write_text("email,groups\na@example.com,ledger\n")
    result = CliRunner().invoke(app, ["access", "migrate", str(tmp_path), "--apply"])
    assert result.exit_code == 2
    assert "Cutover requires" in result.output


def test_standalone_public_preserves_owui_groups_without_custom_model(tmp_path):
    source = _owui_fixture(tmp_path)
    # Intermediate OWUI schema: normalized memberships, legacy model ACL column.
    with source.begin() as c:
        c.execute(text("CREATE TABLE group_member (group_id TEXT,user_id TEXT)"))
        c.execute(text("INSERT INTO group_member VALUES ('g1','u1')"))
    plan = migrate.plan_from_owui(
        source, "hub", standalone_public=True, permissions=["coord"]
    )
    assert not migrate.verify_effective(plan)
    assert ("alice@corp", "hub", "coord") in plan.grants
    assert ("bob@corp", "hub", "coord") not in plan.grants
    assert ("*", "hub", "use_hub") in plan.grants
    assert plan.visibility_backup is None


def test_pending_owui_account_keeps_grants_but_cannot_enter(tmp_path):
    source = _owui_fixture(tmp_path)
    with source.begin() as c:
        c.execute(text("ALTER TABLE \"user\" ADD COLUMN role TEXT DEFAULT 'user'"))
        c.execute(text("UPDATE \"user\" SET role='pending' WHERE id='u1'"))
    plan = migrate.plan_from_owui(
        source, "hub", standalone_public=True, permissions=["coord"]
    )
    assert not migrate.verify_effective(plan)
    gs = GrantStore(create_engine(f"sqlite:///{tmp_path}/target.db"))
    migrate.apply(gs, plan)
    assert not gs.can("alice@corp", "hub", "use_hub")
    gs.upsert_identity(email="alice@corp", owui_id="u1", pending=False)
    assert gs.can("alice@corp", "hub", "coord")
