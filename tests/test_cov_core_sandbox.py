"""Sandbox persisted lifecycle, failed setup, operator refusals and cleanup plans."""
from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import text

from hubzoid import db, migrations
from hubzoid.sandbox import config, service
from hubzoid.work_items.errors import WorkItemError
from tests.test_cov_sandbox_cli import DEPLOYMENT, PROFILE, hub_dir  # noqa: F401


class Provider:
    def __init__(self):
        self.deleted = []
        self.stopped = []
        self.present = True
        self.offline = True
        self.setup_code = 0
        self.leftovers = []

    def prepare(self, profile, **kw):
        return {"image": "fixture"}

    def create(self, sid, deployment, profile, **kw):
        return {"container_id": sid, "image": "fixture", "deployment": deployment,
                "binding": {"kind": "docker", "runtime": "runc"}}

    def container(self, state, sid=None):
        return object() if self.present else None

    def start(self, state):
        return None

    def stop(self, state):
        self.stopped.append(state["container_id"])

    def delete(self, sid, deployment):
        self.deleted.append(sid)
        return 1

    def run_setup(self, *args, **kw):
        return {"exit_code": self.setup_code, "output": "setup output", "timed_out": self.setup_code == 124}

    def go_offline(self, state):
        return {"mode": "deny_all", "verified": self.offline}

    verify_offline = go_offline

    def resources(self, deployment):
        return self.leftovers


@pytest.fixture
def box(hub_dir, monkeypatch):
    (hub_dir / "sandbox.yaml").write_text(DEPLOYMENT)
    (hub_dir / "sandboxes" / "work.yaml").write_text(PROFILE)
    engine = db.operational_engine(hub_dir)
    migrations.upgrade(engine, "operational")
    provider = Provider()
    monkeypatch.setattr(service, "provider", lambda binding: provider)
    monkeypatch.setattr(service, "_LAST_REAP", {})
    yield SimpleNamespace(hub=hub_dir, provider=provider, engine=engine)
    engine.dispose()


def _allocate(box, owner="owner1"):
    return service.allocate(box.hub, owner_kind="probe", owner_id=owner, profile_id="work", actor="cli:test")


def test_allocation_attach_stop_and_release_persist_revision_and_live_ownership(box):
    row = _allocate(box)
    sid = row["id"]
    assert row["state"] == "ready" and row["network_json"]["effective"] == "deny_all"
    assert row["provider_json"]["binding"]["kind"] == "docker"
    assert _allocate(box)["id"] == sid
    same = service.attach(box.hub, sid)
    assert same["revision"] == row["revision"] and same["last_activity"] >= row["last_activity"]
    stopped = service.stop(box.hub, sid)
    assert stopped["state"] == "stopped" and stopped["revision"] > row["revision"]
    resumed = service.attach(box.hub, sid)
    assert resumed["state"] == "ready" and resumed["stopped_at"] is None
    released = service.release(box.hub, sid, "completed")
    assert released["state"] == "deleted" and released["live_key"] is None
    assert service.release(box.hub, sid, "completed") == released
    assert box.provider.deleted == [sid]
    assert service.list_rows(box.hub, live=True) == []
    assert service.list_rows(box.hub, live=False, state="deleted", owner_kind="probe", owner_id="owner1")[0]["id"] == sid


@pytest.mark.parametrize(("state", "reason", "message"), [
    ("deleted", "operator", "administrator removed"), ("deleted", "expired", "time ran out"),
    ("lost", "missing", "disappeared"),
])
def test_gone_sandbox_is_never_recreated(box, state, reason, message):
    row = _allocate(box)
    service._end(box.hub, row["id"], state, reason)
    with pytest.raises(WorkItemError, match=message) as err:
        service.attach(box.hub, row["id"])
    assert err.value.code == "sandbox_lost"
    assert len(service.list_rows(box.hub)) == 1


def test_attach_expired_sandbox_deletes_resources_before_refusal(box):
    row = _allocate(box)
    service._update(box.hub, row["id"], lifetime_deadline=time.time() - 1)
    with pytest.raises(WorkItemError) as err:
        service.attach(box.hub, row["id"])
    assert err.value.code == "lifetime_exceeded"
    assert service.get(box.hub, row["id"])["end_reason"] == "expired"
    assert box.provider.deleted == [row["id"]]


@pytest.mark.parametrize("operation", ["attach", "stop"])
def test_missing_container_becomes_lost(box, operation):
    row = _allocate(box)
    box.provider.present = False
    if operation == "attach":
        with pytest.raises(WorkItemError) as err:
            service.attach(box.hub, row["id"])
        assert err.value.code == "sandbox_lost"
    else:
        assert service.stop(box.hub, row["id"])["state"] == "lost"
    stored = service.get(box.hub, row["id"])
    assert stored["state"] == "lost" and stored["live_key"] is None


def test_resumed_sandbox_with_network_is_deleted(box):
    row = _allocate(box)
    service.stop(box.hub, row["id"])
    box.provider.offline = False
    with pytest.raises(WorkItemError, match="no longer offline") as err:
        service.attach(box.hub, row["id"])
    assert err.value.code == "network_mode_unsupported"
    assert service.get(box.hub, row["id"])["state"] == "deleted"


@pytest.mark.parametrize(("setup_code", "offline", "code"), [
    (124, True, "setup_failed"), (0, False, "network_mode_unsupported"),
])
def test_failed_setup_or_network_verification_records_failure_and_removes_container(box, setup_code, offline, code):
    path = box.hub / "sandboxes" / "work.yaml"
    path.write_text(PROFILE + "setup:\n  command: echo ready\n")
    box.provider.setup_code = setup_code
    box.provider.offline = offline
    with pytest.raises(WorkItemError) as err:
        _allocate(box)
    assert err.value.code == code
    row = service.list_rows(box.hub)[0]
    assert row["state"] == "setup_failed" and row["live_key"] is None
    assert box.provider.deleted == [row["id"]]
    assert "setup output" not in row["error"]
    if setup_code:
        assert row["setup_json"]["timed_out"] is True


def test_provider_start_exception_is_recorded_even_if_cleanup_fails(box, monkeypatch):
    monkeypatch.setattr(box.provider, "prepare", Mock(side_effect=RuntimeError("image failed\nprivate output")))
    monkeypatch.setattr(box.provider, "delete", Mock(side_effect=OSError("engine down")))
    with pytest.raises(RuntimeError, match="image failed"):
        _allocate(box)
    row = service.list_rows(box.hub)[0]
    assert row["state"] == "start_failed" and row["error"] == "RuntimeError: image failed"


def test_interrupted_allocation_is_replaced_only_after_recording_failure(box):
    row = _allocate(box)
    service._update(box.hub, row["id"], state="setting_up")
    new = _allocate(box)
    assert new["id"] != row["id"] and new["state"] == "ready"
    old = service.get(box.hub, row["id"])
    assert old["state"] == "setup_failed" and old["end_reason"] == "interrupted"
    assert row["id"] in box.provider.deleted


def test_capacity_and_revision_conflicts_leave_live_sandbox_unchanged(box):
    path = box.hub / "sandbox.yaml"
    path.write_text(DEPLOYMENT + "  limits:\n    max_active_per_hub: 1\n")
    row = _allocate(box)
    with pytest.raises(WorkItemError) as err:
        _allocate(box, "second-owner")
    assert err.value.code == "capacity_exceeded" and err.value.status == 429
    with pytest.raises(WorkItemError) as err:
        service._update(box.hub, row["id"], expected_revision=0, state="deleted")
    assert err.value.code == "revision_conflict"
    assert service.get(box.hub, row["id"])["state"] == "ready"


def test_operator_permission_and_stale_revision_refuse_before_mutation(box, monkeypatch):
    from hubzoid.work_items import service as work

    row = _allocate(box)
    monkeypatch.setattr(work, "_can", lambda *a: False)
    with pytest.raises(WorkItemError) as err:
        service.operator_stop(box.hub, row["id"], "member@example.org", reason="test")
    assert err.value.code == "permission_denied" and err.value.status == 403
    for operation in (service.operator_stop, service.operator_delete):
        with pytest.raises(WorkItemError) as err:
            operation(box.hub, row["id"], "cli:test", reason="test", expected_revision=0)
        assert err.value.code == "revision_conflict"
    assert service.get(box.hub, row["id"])["state"] == "ready"


def test_release_when_provider_unavailable_still_finishes_owner_record(box, monkeypatch):
    row = _allocate(box)
    monkeypatch.setattr(service, "_provider_for", Mock(side_effect=WorkItemError("docker_unavailable", "offline")))
    result = service.release(box.hub, row["id"], "completed")
    assert result["state"] == "deleted" and result["live_key"] is None
    with pytest.raises(WorkItemError) as err:
        service.require(box.hub, "does-not-exist")
    assert err.value.code == "not_found"


def test_provider_binding_is_pinned_and_missing_legacy_binding_is_named(box, monkeypatch):
    calls = []
    monkeypatch.setattr(service, "provider", lambda binding: calls.append(binding) or box.provider)
    assert service._provider_for(box.hub, {"provider_json": {"binding": {"endpoint": "old-engine"}}}) is box.provider
    assert calls == [{"endpoint": "old-engine"}]
    with pytest.raises(WorkItemError, match="no longer in sandbox.yaml") as err:
        service._provider_for(box.hub, {"provider_binding": "removed"})
    assert err.value.code == "provider_missing"


def test_reaper_dry_run_plans_idle_expired_interrupted_and_orphan_resources(box, monkeypatch):
    rows = [_allocate(box, f"owner{n}") for n in range(3)]
    now = time.time()
    service._update(box.hub, rows[0]["id"], last_activity=now - 100)
    service._update(box.hub, rows[1]["id"], lifetime_deadline=now - 1)
    service._update(box.hub, rows[2]["id"], state="setting_up", updated=now - 2000)
    # _update always stamps updated, so model an interrupted process's stored row.
    with box.engine.begin() as conn:
        conn.execute(text("UPDATE hz_sandboxes SET updated=:t WHERE id=:i"), {"t": now - 2000, "i": rows[2]["id"]})
    box.provider.leftovers = [{"sandbox_id": "orphan", "created": None, "kind": "container", "role": "box"},
        {"sandbox_id": "orphan", "created": None, "kind": "network", "role": "network"},
        {"sandbox_id": "too-new", "created": now, "kind": "container", "role": "box"}]
    monkeypatch.setattr(service, "_owner_done", lambda *a: False)
    plan = service.reap(box.hub)
    assert plan["dry_run"] is True
    assert plan["stop"] == [{"id": rows[0]["id"], "reason": "idle"}]
    assert plan["release"] == [{"id": rows[1]["id"], "reason": "expired"}]
    assert plan["interrupted"] == [{"id": rows[2]["id"], "reason": "interrupted"}]
    assert plan["orphans"] == [{"id": "orphan", "kind": "container", "role": "box"}]
    assert box.provider.deleted == [] and box.provider.stopped == []
    done = service.reap(box.hub, dry_run=False)
    assert done["stop"] == plan["stop"]
    assert service.get(box.hub, rows[0]["id"])["state"] == "stopped"
    assert service.get(box.hub, rows[1]["id"])["state"] == "deleted"
    assert service.get(box.hub, rows[2]["id"])["state"] == "setup_failed"
    assert "orphan" in box.provider.deleted
    assert service._LAST_REAP[box.hub.name] == {"at": done["at"], "orphans": 1, "released": 2, "stopped": 1}


def test_unready_attach_and_stopping_unready_sandbox_do_not_start_it(box):
    row = _allocate(box)
    row = service._update(box.hub, row["id"], state="allocating")
    with pytest.raises(WorkItemError) as err:
        service.attach(box.hub, row["id"])
    assert err.value.code == "sandbox_unavailable"
    assert service.stop(box.hub, row["id"]) == row


def test_profile_disabled_and_empty_probe_refuse_without_resources(box):
    with pytest.raises(WorkItemError) as err:
        service.probe(box.hub, "work", [])
    assert err.value.code == "invalid_request"
    profile = config.load_profile(box.hub, "work")
    (box.hub / "sandbox.yaml").unlink()
    with pytest.raises(WorkItemError) as err:
        config.check_profile(box.hub, profile)
    assert err.value.code == "sandbox_disabled"
    assert service.list_rows(box.hub) == []
