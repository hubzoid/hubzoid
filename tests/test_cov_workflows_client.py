"""Operator commands hand work to the live engine (hubzoid.workflows.client):
which queue, partition and arguments each kind of run gets, that nothing is
queued when no engine serves the hub, that the DBOS client is always closed,
and what waiting on a run does when its owner stops. DBOS is a fake here."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from hubzoid.workflows import client as wc
from hubzoid.workflows.runtime import _app_name

READY = {"definitions": {"report": {"queue": "q-report", "manual_partition": "report"},
                         "shared": {"queue": "q-shared", "manual_partition": "team"},
                         "plain": {"queue": "q-plain"}}}


class FakeClient:
    made: list = []

    def __init__(self, **kw):
        self.kw, self.enqueued, self.destroyed = kw, [], False
        FakeClient.made.append(self)

    def enqueue(self, options, *args):
        self.enqueued.append((options, args))
        return SimpleNamespace(get_workflow_id=lambda: options["workflow_id"])

    def destroy(self):
        self.destroyed = True


@pytest.fixture
def hub(tmp_path, monkeypatch):
    import dbos

    FakeClient.made = []
    monkeypatch.setattr(dbos, "DBOSClient", FakeClient)
    monkeypatch.setattr(wc.db, "dbos_url", lambda hub_dir: "sqlite:///fake")
    h = tmp_path / "ops"
    h.mkdir()
    return h


def test_nothing_is_queued_without_a_live_engine(hub, monkeypatch):
    monkeypatch.setattr(wc, "readiness", lambda hub_dir, name: None)
    assert wc.enqueue_live(hub, "report") is None
    assert wc.enqueue_personal(hub, "report", envelope={"acts_as": "a@x"}, workflow_id="w1") is None
    assert FakeClient.made == []


def test_live_runs_get_their_queue_partition_and_arguments(hub, monkeypatch):
    monkeypatch.setattr(wc, "readiness", lambda hub_dir, name: READY)
    handle = wc.enqueue_live(hub, "report", envelope={"acts_as": "a@x"})
    options, args = FakeClient.made[-1].enqueued[0]
    assert options["workflow_name"] == "report" and options["queue_name"] == "q-report"
    assert options["queue_partition_key"] == "report" and args == ("ops", None, {"acts_as": "a@x"})
    assert options["workflow_id"].startswith("manual:") and options["workflow_id"].endswith("@" + _app_name("ops"))
    assert handle.get_workflow_id() == options["workflow_id"]

    wc.enqueue_live(hub, "plain")
    options, args = FakeClient.made[-1].enqueued[0]
    assert options["queue_partition_key"] == "plain" and args == ("ops",)  # defaults to the workflow name

    from hubzoid.workflows.markdown import MD_WORKFLOW

    wc.enqueue_live(hub, "weekly", markdown=True, overrides={"to": "ops"})
    options, args = FakeClient.made[-1].enqueued[0]
    assert options["workflow_name"] == MD_WORKFLOW and options["queue_name"] == _app_name("ops") + "-md"
    assert options["workflow_id"].startswith("md:weekly:manual-") and args == ("weekly", [], {"to": "ops"})


def test_an_unknown_workflow_is_refused_and_the_client_closed(hub, monkeypatch):
    monkeypatch.setattr(wc, "readiness", lambda hub_dir, name: READY)
    with pytest.raises(ValueError, match="not served by the live owner"):
        wc.enqueue_live(hub, "missing")
    assert FakeClient.made[-1].destroyed is True
    with pytest.raises(ValueError, match="not served by the live owner"):
        wc.enqueue_personal(hub, "missing", envelope={"acts_as": "a@x"}, workflow_id="w")


def test_personal_runs_partition_per_person_unless_shared(hub, monkeypatch):
    monkeypatch.setattr(wc, "readiness", lambda hub_dir, name: READY)
    assert wc.enqueue_personal(hub, "report", envelope={"acts_as": "ana@x"}, workflow_id="w1") == "w1"
    options, args = FakeClient.made[-1].enqueued[0]
    assert options["queue_partition_key"] == "report:ana@x" and args == ("ops", None, {"acts_as": "ana@x"})
    assert FakeClient.made[-1].destroyed is True

    wc.enqueue_personal(hub, "shared", envelope={"acts_as": "ana@x"}, workflow_id="w2")
    assert FakeClient.made[-1].enqueued[0][0]["queue_partition_key"] == "team"
    wc.enqueue_personal(hub, "plain", envelope={"acts_as": "ana@x"}, workflow_id="w3")
    assert "queue_partition_key" not in FakeClient.made[-1].enqueued[0][0]


def test_waiting_returns_the_result_or_says_the_owner_stopped(hub, monkeypatch):
    monkeypatch.setattr(wc.time, "sleep", lambda s: None)
    statuses = iter(["PENDING", "SUCCESS"])
    handle = SimpleNamespace(get_status=lambda: SimpleNamespace(status=next(statuses)),
                             get_result=lambda: {"ok": True}, get_workflow_id=lambda: "run-1")
    monkeypatch.setattr(wc, "readiness", lambda hub_dir, name: READY)
    client = FakeClient()
    assert wc.LiveHandle(client, handle, hub).get_result() == {"ok": True} and client.destroyed

    pending = SimpleNamespace(get_status=lambda: SimpleNamespace(status="PENDING"),
                              get_result=lambda: None, get_workflow_id=lambda: "run-2")
    monkeypatch.setattr(wc, "readiness", lambda hub_dir, name: None)
    client = FakeClient()
    with pytest.raises(RuntimeError, match="run-2 remains durable"):
        wc.LiveHandle(client, pending, hub).get_result()
    assert client.destroyed
