"""Workflow ownership contracts, with local SQLite and no model calls."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest
from sqlalchemy import inspect, text

from hubzoid import db, migrations
from hubzoid.workflows.ownership import Owner, OwnerBusy, OwnershipLost, readiness


def test_step_rejects_stale_owner_and_expired_deadline_before_side_effect(monkeypatch):
    from hubzoid.workflows import runtime, deadlines

    monkeypatch.setattr(runtime, "_INITED", True)
    monkeypatch.setattr(runtime, "_DBOS", SimpleNamespace(step=lambda **_: lambda fn: fn))
    effects = []

    @runtime.step()
    def side_effect():
        effects.append("ran")

    def stale():
        raise RuntimeError("stale owner")

    monkeypatch.setattr(runtime, "_OWNER", SimpleNamespace(assert_owned=stale))
    with pytest.raises(RuntimeError, match="stale owner"):
        side_effect()
    monkeypatch.setattr(runtime, "_OWNER", SimpleNamespace(assert_owned=lambda: None))
    with pytest.raises(TimeoutError, match="deadline exceeded"):
        with deadlines.scope(0.01):
            time.sleep(0.02)
            side_effect()
    assert effects == []


@pytest.fixture
def local_hub(tmp_path, monkeypatch):
    for key in ("DATABASE_URL", "HUBZOID_DBOS_DB", "HUBZOID_OPERATIONAL_DB"):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


def test_owner_mutual_exclusion_even_after_lease_expires(local_hub):
    first = Owner(local_hub, "test-hub").acquire()
    try:
        with first.engine.begin() as conn:
            conn.execute(text("UPDATE hz_workflow_owner SET expires=0 WHERE hub='test-hub'"))
        assert readiness(local_hub, "test-hub") is None
        with pytest.raises(OwnerBusy):
            Owner(local_hub, "test-hub").acquire()
    finally:
        first.close()

    second = Owner(local_hub, "test-hub").acquire()
    try:
        assert second.generation == first.generation + 1
        assert second.boot != first.boot
    finally:
        second.close()


def test_readiness_requires_live_lease_and_clears_on_close(local_hub):
    owner = Owner(local_hub, "test-hub").acquire()
    try:
        assert readiness(local_hub, "test-hub") is None
        owner.heartbeat({"version": "v1", "webhooks": {"created": "handler"}})
        ready = readiness(local_hub, "test-hub")
        assert ready == {"version": "v1", "webhooks": {"created": "handler"},
                         "boot": owner.boot, "generation": owner.generation}
        with owner.engine.begin() as conn:
            conn.execute(text("UPDATE hz_workflow_owner SET expires=:past WHERE hub='test-hub'"),
                         {"past": time.time() - 1})
        assert readiness(local_hub, "test-hub") is None
    finally:
        owner.close()
    assert readiness(local_hub, "test-hub") is None


def test_stale_fencing_token_rejected(local_hub):
    owner = Owner(local_hub, "test-hub").acquire()
    told = []
    owner.on_lost(told.append)
    try:
        with owner.engine.begin() as conn:
            conn.execute(text("UPDATE hz_workflow_owner SET generation=generation+1 "
                              "WHERE hub='test-hub'"))
        # Not an Exception: DBOS must not record it as the run's outcome.
        with pytest.raises(OwnershipLost, match="fencing token is stale"):
            owner.heartbeat({"version": "stale"})
        assert not issubclass(OwnershipLost, Exception)
        with pytest.raises(OwnershipLost):
            owner.assert_owned()
        assert owner.lost and told == ["Workflow owner fencing token is stale"]
    finally:
        owner.close()


def test_operational_migration_creates_owner_event_and_alert_tables(local_hub):
    engine = db.operational_engine(local_hub)
    migrations.upgrade(engine, "operational")
    schema = inspect(engine)
    for table in ("hz_workflow_owner", "hz_workflow_events", "hz_workflow_alerts"):
        assert table in schema.get_table_names()
    assert {"hub", "boot", "generation", "expires", "ready"} <= {
        column["name"] for column in schema.get_columns("hz_workflow_owner")
    }
