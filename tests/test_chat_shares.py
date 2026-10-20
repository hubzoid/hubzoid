"""Pre-2.0 read-only links (hz_shares): readable by signed-in people while the
owner is active, turned off by the owner; nothing makes or refreshes them."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.chat_helpers import as_people, build_app, chat_body, make_hub, wait_titles, who


@pytest.fixture
def setup(tmp_path, monkeypatch):
    hub = make_hub(tmp_path, monkeypatch, SHOW_THINKING="full")
    as_people(monkeypatch)
    from hubzoid.auth import users
    from tests.chat_helpers import PEOPLE
    for person in PEOPLE.values():
        users.create(hub, email=person.email, id=person.id, name=person.name)
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


def legacy(app, conv_id="c_share0001", owner="u_ana", **extra) -> str:
    """A link as a 1.x build made it: a stored snapshot of the active branch."""
    from hubzoid.chat.shares import snapshot

    store = app.state.chat.store
    snap = dict(snapshot(store, store.get_conversation(conv_id), "Ana Example"), **extra)
    conv = store.get_conversation(conv_id)
    return store.save_share(conversation_id=conv_id, owner_id=owner, title=conv.get("title"),
                            agent=conv.get("agent"), snapshot=snap)["id"]


@pytest.mark.parametrize("public_url,origin", [("", ""), ("https://hub.example.com/b/finance", "https://hub.example.com")])
def test_an_old_link_reads_for_anyone_signed_in(setup, monkeypatch, public_url, origin):
    app, client = setup
    monkeypatch.setenv("HUBZOID_PUBLIC_URL", public_url)
    share_id = legacy(app)
    mine = client.get("/api/conversations/c_share0001/share", headers=who("ana")).json()
    assert mine == {"share_id": share_id, "url": f"{origin}/s/{share_id}"}
    shared = client.get(f"/api/shares/{share_id}", headers=who("ben"))
    assert shared.status_code == 200
    body = shared.json()
    assert body["owner_name"] == "Ana Example" and body["agent"] == app.state.chat.model_label
    user, reply = body["messages"]
    assert user["content"][1] == {"type": "file", "name": "brief.txt", "mime": "text/plain", "size": 5}
    assert "file_id" not in str(body)
    tool = next(p for p in reply["content"] if p["type"] == "tool-call")
    assert tool == {"type": "tool-call", "toolCallId": "call_tool", "toolName": "read_knowledge",
                    "result": {"status": "ok"}}
    # 1.x kept the reasoning; reads leave it out now.
    assert [p["type"] for p in reply["content"]] == ["tool-call", "text"]


def test_no_new_or_refreshed_old_style_links(setup):
    _, client = setup
    r = client.post("/api/conversations/c_share0001/share", headers=who("ana"))
    assert r.status_code in (404, 405)


def test_an_old_link_loses_its_signed_download_links(setup):
    app, client = setup
    share_id = legacy(app)
    store = app.state.chat.store
    share = store.get_share(share_id)
    snap = share["snapshot"]
    snap["messages"][-1]["content"].append(
        {"type": "text", "text": "[Download out.xlsx](https://h.example/artifacts/web-c_share0001/out.xlsx?t=sig&e=1)"})
    store.save_share(conversation_id="c_share0001", owner_id="u_ana", title=share["title"],
                     agent=share["agent"], snapshot=snap)
    text = str(client.get(f"/api/shares/{share_id}", headers=who("ben")).json())
    assert "t=sig" not in text and "out.xlsx" in text


def test_signed_out_people_cannot_read_a_share(setup):
    app, client = setup
    share_id = legacy(app)
    assert client.get(f"/api/shares/{share_id}").status_code == 401
    assert client.get("/api/shares/not-a-real-share-id", headers=who("ben")).status_code == 404
    assert client.get("/api/shares/x", headers=who("ben")).status_code == 404


@pytest.mark.parametrize("audience", ["owner", "restricted", None])
def test_imported_private_links_stay_closed(setup, audience):
    app, client = setup
    share_id = legacy(app, source="openwebui", audience=audience)
    for person in ("ana", "ben"):
        assert client.get(f"/api/shares/{share_id}", headers=who(person)).status_code == 404
    assert client.get("/api/conversations/c_share0001/share", headers=who("ana")).status_code == 404
    assert app.state.chat.store.get_share(share_id)["snapshot"]["audience"] == audience  # retained, not public


def test_only_the_owner_turns_the_link_off(setup):
    app, client = setup
    share_id = legacy(app)
    ben = who("ben")
    assert client.delete("/api/conversations/c_share0001/share", headers=ben).status_code == 404
    assert client.get("/api/conversations/c_share0001/share", headers=ben).status_code == 404
    assert client.delete("/api/conversations/c_share0001/share",
                         headers={"X-Test-User": "ana"}).status_code == 403   # no Origin
    assert client.delete("/api/conversations/c_share0001/share", headers=who("ana")).status_code == 204
    assert client.get(f"/api/shares/{share_id}", headers=ben).status_code == 404
    r = client.get("/api/conversations/c_share0001/share", headers=who("ana"))
    assert r.status_code == 404 and r.json()["detail"]["code"] == "not_shared"


def test_deleted_account_share_is_not_readable(setup):
    from hubzoid.auth import users
    app, client = setup
    hub = app.state.chat.hub_dir
    owner = users.get(hub, 'u_ana')
    share = legacy(app)
    from hubzoid.access.accounts import HubzoidAccounts
    # The production adapter called by Console deletion.
    HubzoidAccounts(hub).delete(owner['id'])
    from hubzoid.access import store_for
    store_for(hub).revoke_all('ana@example.org', actor='admin@localhost')
    store_for(hub).mark_account_removed('ana@example.org', actor='admin@localhost')
    # Deleting keeps a tombstone (chats stay attributable), never a live account.
    assert users.store(hub).get('u_ana')['status'] == 'deleted'
    response = client.get('/api/shares/' + share, headers=who('ben'))
    assert response.status_code == 404, response.text
    from sqlalchemy import text
    with app.state.chat.store.engine.connect() as conn:
        # The conversation stays with the tombstone; only its share links go.
        assert conn.execute(text("SELECT count(*) FROM hz_conversations WHERE owner_id='u_ana'")).scalar() == 1
        assert conn.execute(text("SELECT count(*) FROM hz_shares WHERE owner_id='u_ana'")).scalar() == 0


def test_a_blocked_owners_old_link_stays_closed(setup):
    from hubzoid.access import store_for
    app, client = setup
    share = legacy(app)
    store_for(app.state.chat.hub_dir).suspend("ana@example.org", actor="admin@localhost")
    assert client.get('/api/shares/' + share, headers=who('ben')).status_code == 404


def test_legacy_share_with_deleted_owner_stays_closed(setup):
    from sqlalchemy import text
    app, client = setup
    share = legacy(app)
    # Simulate an orphan left by an older version, before transactional cleanup.
    with app.state.chat.store.engine.begin() as conn:
        conn.execute(text("DELETE FROM hz_users WHERE id='u_ana'"))
    assert client.get('/api/shares/' + share, headers=who('ben')).status_code == 404
