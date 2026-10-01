"""Read-only shares: a snapshot of the active branch, readable by any signed-in
person of the deployment, created and revoked by the owner only."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.chat_helpers import as_people, build_app, chat_body, make_hub, wait_titles, who


@pytest.fixture
def setup(tmp_path, monkeypatch):
    hub = make_hub(tmp_path, monkeypatch, SHOW_THINKING="full")
    as_people(monkeypatch)
    app = build_app()
    client = TestClient(app)
    agent = app.state.chat.model_label
    ana = who("ana")
    client.post("/api/conversations", headers=ana, json={"agent": agent, "id": "c_share0001"})
    up = client.post("/api/conversations/c_share0001/files", headers=ana,
                     files={"file": ("brief.txt", b"brief", "text/plain")}).json()
    client.post("/api/chat", headers=ana, json=chat_body(
        "c_share0001", "think and use a tool", agent=None, message_id="m_user00001",
        assistant_id="m_asst00001", files=(up["file_id"],)))
    wait_titles()
    return app, client


def test_owner_shares_and_anyone_signed_in_reads(setup):
    app, client = setup
    r = client.post("/api/conversations/c_share0001/share", headers=who("ana"))
    assert r.status_code == 200
    share_id, url = r.json()["share_id"], r.json()["url"]
    assert url == f"/s/{share_id}" and len(share_id) >= 16
    assert client.get("/api/conversations/c_share0001/share", headers=who("ana")).json()["share_id"] == share_id
    shared = client.get(f"/api/shares/{share_id}", headers=who("ben"))
    assert shared.status_code == 200
    body = shared.json()
    assert body["owner_name"] == "Ana Example" and body["agent"] == app.state.chat.model_label
    assert body["title"] == "About think and use a"
    user, reply = body["messages"]
    assert user["role"] == "user" and reply["role"] == "assistant"
    # attachments keep their names, never a way to fetch them
    assert user["content"][1] == {"type": "file", "name": "brief.txt", "mime": "text/plain", "size": 5}
    assert "file_id" not in str(body)
    # which tools ran and how they ended, not their arguments
    tool = next(p for p in reply["content"] if p["type"] == "tool-call")
    assert tool == {"type": "tool-call", "toolCallId": "call_tool", "toolName": "read_knowledge",
                    "result": {"status": "ok"}}
    assert [p["type"] for p in reply["content"]] == ["reasoning", "tool-call", "text"]


def test_signed_out_people_cannot_read_a_share(setup):
    _, client = setup
    share_id = client.post("/api/conversations/c_share0001/share", headers=who("ana")).json()["share_id"]
    assert client.get(f"/api/shares/{share_id}").status_code == 401
    assert client.get("/api/shares/not-a-real-share-id", headers=who("ben")).status_code == 404
    assert client.get("/api/shares/x", headers=who("ben")).status_code == 404


def test_the_snapshot_is_frozen_until_shared_again(setup):
    _, client = setup
    ana = who("ana")
    share_id = client.post("/api/conversations/c_share0001/share", headers=ana).json()["share_id"]
    client.post("/api/chat", headers=ana, json=chat_body(
        "c_share0001", "a later question", agent=None, message_id="m_user00002",
        assistant_id="m_asst00002", parent_id="m_asst00001"))
    assert len(client.get(f"/api/shares/{share_id}", headers=who("ben")).json()["messages"]) == 2
    again = client.post("/api/conversations/c_share0001/share", headers=ana).json()
    assert again["share_id"] == share_id   # one link per conversation
    assert len(client.get(f"/api/shares/{share_id}", headers=who("ben")).json()["messages"]) == 4


def test_the_active_branch_is_shared(setup):
    _, client = setup
    ana = who("ana")
    client.post("/api/chat", headers=ana, json={
        "conversation_id": "c_share0001", "parent_id": "m_user00001",
        "assistant_message_id": "m_asst00001b"})
    client.patch("/api/conversations/c_share0001", headers=ana, json={"head_id": "m_asst00001"})
    share_id = client.post("/api/conversations/c_share0001/share", headers=ana).json()["share_id"]
    ids = [m["id"] for m in client.get(f"/api/shares/{share_id}", headers=ana).json()["messages"]]
    assert ids == ["m_user00001", "m_asst00001"]


def test_only_the_owner_manages_the_link(setup):
    _, client = setup
    share_id = client.post("/api/conversations/c_share0001/share", headers=who("ana")).json()["share_id"]
    ben = who("ben")
    assert client.post("/api/conversations/c_share0001/share", headers=ben).status_code == 404
    assert client.delete("/api/conversations/c_share0001/share", headers=ben).status_code == 404
    assert client.get("/api/conversations/c_share0001/share", headers=ben).status_code == 404
    assert client.delete("/api/conversations/c_share0001/share",
                         headers={"X-Test-User": "ana"}).status_code == 403   # no Origin
    assert client.delete("/api/conversations/c_share0001/share", headers=who("ana")).status_code == 204
    assert client.get(f"/api/shares/{share_id}", headers=ben).status_code == 404
    r = client.get("/api/conversations/c_share0001/share", headers=who("ana"))
    assert r.status_code == 404 and r.json()["detail"]["code"] == "not_shared"


def test_an_empty_conversation_has_nothing_to_share(setup):
    app, client = setup
    client.post("/api/conversations", headers=who("ana"),
                json={"agent": app.state.chat.model_label, "id": "c_empty0001"})
    r = client.post("/api/conversations/c_empty0001/share", headers=who("ana"))
    assert r.status_code == 409 and r.json()["detail"]["code"] == "nothing_to_share"


def test_share_url_uses_the_public_address(setup, monkeypatch):
    _, client = setup
    monkeypatch.setenv("HUBZOID_PUBLIC_URL", "https://hub.example.org/")
    url = client.post("/api/conversations/c_share0001/share", headers=who("ana")).json()["url"]
    assert url.startswith("https://hub.example.org/s/")
