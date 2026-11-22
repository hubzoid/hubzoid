"""Hub changes and personal memory over HTTP (hubzoid.hub_changes.routes):
someone who may only use the hub never sees proposals; proposing and
applying need in-app editing turned on (rejecting doesn't); a curator
proposes, only a manager decides, never with a bearer token; a change can be
undone as a new proposal; each person's notes are their own, compare-and-set,
and can be deleted. Uses the hub of tests/test_hub_changes.py."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from hubzoid.auth import AuthUser
from tests import chat_helpers
from tests.chat_helpers import as_people, build_app, who
from tests.test_hub_changes import OWNER, RULE, hub  # noqa: F401


@pytest.fixture
def client(hub, monkeypatch):  # noqa: F811
    monkeypatch.setitem(chat_helpers.PEOPLE, "owner", AuthUser(id="u_owner", email=OWNER, name="Owner", role="user",
                                                               method="password"))
    monkeypatch.delenv("HUBZOID_IN_APP_EDITING", raising=False)
    as_people(monkeypatch)
    with TestClient(build_app()) as c:
        yield c


def test_proposals_need_editing_on_and_only_a_manager_decides(client, monkeypatch):
    ana, ben, owner = who("ana"), who("ben"), who("owner")
    r = client.get("/api/hub-changes", headers=ben)
    assert r.status_code == 403 and "may change this hub's knowledge" in r.json()["detail"]["message"]
    off = client.post("/api/hub-changes", headers=ana, json={"title": "Second check", "edits": RULE})
    assert off.status_code == 409
    assert client.get("/api/editing", headers=ana).json()["in_app_editing"] is False

    monkeypatch.setenv("HUBZOID_IN_APP_EDITING", "1")
    made = client.post("/api/hub-changes", headers=ana, json={"title": "Second check", "edits": RULE,
                                                              "reason": "audit asked"})
    assert made.status_code == 200 and made.json()["status"] == "pending"
    cid = made.json()["id"]
    bad = client.post("/api/hub-changes", headers=ana, json={"title": "x", "edits": [{"path": "../etc/passwd"}]})
    assert (bad.status_code, bad.json()["detail"]["code"]) == (400, "invalid_change")
    listed = client.get("/api/hub-changes?status=pending", headers=ana).json()
    assert [c["id"] for c in listed["items"]] == [cid] and listed["can_review"] is False
    one = client.get(f"/api/hub-changes/{cid}", headers=owner).json()
    assert one["can_review"] is True and "second check" in one["diff"]
    assert client.get("/api/hub-changes/99999", headers=owner).status_code == 404

    keyed = {**owner, "authorization": "Bearer hzk_test"}
    r = client.post(f"/api/hub-changes/{cid}/decision", headers=keyed, json={"decision": "approve"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "human_action_required"
    r = client.post(f"/api/hub-changes/{cid}/decision", headers=ana,
                    json={"decision": "approve", "expected_revision": one["revision"]})
    assert r.status_code == 403
    done = client.post(f"/api/hub-changes/{cid}/decision", headers=owner,
                       json={"decision": "approve", "expected_revision": one["revision"]}).json()
    assert done["status"] == "applied"
    back = client.post(f"/api/hub-changes/{cid}/undo", headers=owner).json()
    assert back["status"] == "pending" and back["id"] != cid

    monkeypatch.delenv("HUBZOID_IN_APP_EDITING")
    rejected = client.post(f"/api/hub-changes/{back['id']}/decision", headers=owner,
                           json={"decision": "reject", "expected_revision": back["revision"], "note": "keep it"})
    assert rejected.status_code == 200 and rejected.json()["status"] == "rejected"   # editing off: still allowed


def test_each_persons_notes_are_their_own(client):
    ana, ben = who("ana"), who("ben")
    assert client.get("/api/memory", headers=ana).json() == {"text": "", "revision": 0, "updated_at": None}
    saved = client.put("/api/memory", headers=ana, json={"text": "I prefer tables.", "expected_revision": 0}).json()
    assert saved["text"] == "I prefer tables." and saved["revision"] == 1
    stale = client.put("/api/memory", headers=ana, json={"text": "x", "expected_revision": 0})
    assert stale.status_code == 409
    assert client.get("/api/memory", headers=ben).json()["text"] == ""
    long = client.put("/api/memory", headers=ana, json={"text": "x" * 100_000})
    assert long.status_code == 400 and long.json()["detail"]["code"] == "too_long"
    client.delete("/api/memory", headers=ana)
    assert client.get("/api/memory", headers=ana).json()["text"] == ""

