"""Workflow access over HTTP (hubzoid.workflows.access_routes): someone who
manages access reads the policy and the people who use the hub, validates,
diffs and applies a document (compare-and-set); anyone signed in lists the
workflows with their own actions and asks about themselves, and is refused
the rest. Uses the hub and policy of tests/test_workflow_access.py."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hubzoid import access
from tests.chat_helpers import as_people, build_app, who
from tests.test_workflow_access import POLICY, hub  # noqa: F401


@pytest.fixture
def client(hub, monkeypatch):  # noqa: F811
    gs = access.store_for(hub)
    gs.grant("ana@example.org", "*", "manage_access", actor="test")
    gs.grant("ben@example.org", hub.name, "use_hub", actor="test")
    as_people(monkeypatch)
    with TestClient(build_app()) as c:
        yield c


def test_a_manager_reads_validates_diffs_and_applies(client):
    ana = who("ana")
    got = client.get("/api/workflow-access", headers=ana).json()
    assert got["revision"] == 0 and got["grants"] == [] and "run" in got["actions"]
    people = [p["subject"] for p in client.get("/api/workflow-access/subjects", headers=ana).json()["items"]]
    assert "ben@example.org" in people and people[-1] == "everyone"
    assert client.post("/api/workflow-access/validate", headers=ana, json={"yaml": POLICY}).json() == \
        {"valid": True, "grants": 5}
    bad = client.post("/api/workflow-access/validate", headers=ana, json={"yaml": "version: 9"}).json()
    assert bad["valid"] is False and bad["error"]["code"] == "invalid_policy"
    diff = client.post("/api/workflow-access/diff", headers=ana, json={"yaml": POLICY}).json()
    assert len(diff["added"]) == 5 and diff["removed"] == []
    r = client.post("/api/workflow-access/diff", headers=ana, json={"yaml": POLICY.replace("weekly_report", "nope", 1)})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "unknown_workflow"
    put = client.put("/api/workflow-access", headers=ana, json={"yaml": POLICY, "expected_revision": 0})
    assert put.status_code == 200 and put.json()["revision"] == 1
    stale = client.put("/api/workflow-access", headers=ana, json={"yaml": POLICY, "expected_revision": 0})
    assert stale.status_code == 409 and stale.json()["detail"]["current_revision"] == 1


def test_anyone_signed_in_sees_their_own_actions_and_nothing_else(client):
    ana, ben = who("ana"), who("ben")
    client.put("/api/workflow-access", headers=ana, json={"yaml": POLICY, "expected_revision": 0})
    items = {r["key"]: r for r in client.get("/api/workflow-access/resources", headers=ben).json()["items"]}
    assert set(items["weekly_report"]["allowed_actions"]) == {"view_definition", "edit"}
    mine = client.post("/api/workflow-access/explain", headers=ben,
                       json={"workflow": "weekly_report", "action": "run"}).json()
    assert mine["subject"] == "ben@example.org" and mine["allowed"] is False
    other = client.post("/api/workflow-access/explain", headers=ben,
                        json={"subject": "ana@example.org", "workflow": "weekly_report", "action": "run"})
    assert other.status_code == 403 and other.json()["detail"]["code"] == "permission_denied"
    for method, path in (("get", "/api/workflow-access"), ("get", "/api/workflow-access/subjects"),
                         ("post", "/api/workflow-access/validate"), ("post", "/api/workflow-access/diff"),
                         ("put", "/api/workflow-access")):
        r = getattr(client, method)(path, headers=ben, **({} if method == "get" else {"json": {"yaml": POLICY}}))
        assert r.status_code == 403 and "manages access" in r.json()["detail"]["message"], path
