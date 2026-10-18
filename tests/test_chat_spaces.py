"""Team spaces, notes and calendar (HUB-80, first part)."""
from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from tests.chat_helpers import ORIGIN, PEOPLE, as_people, build_app, make_hub, who


@pytest.fixture
def hub(tmp_path, monkeypatch):
    import dataclasses

    from hubzoid.auth import users

    hub = make_hub(tmp_path, monkeypatch)
    for key, p in list(PEOPLE.items()):
        u = users.create(hub, email=p.email, name=p.name)
        monkeypatch.setitem(PEOPLE, key, dataclasses.replace(p, id=u["id"]))
    return hub


@pytest.fixture
def client(hub, monkeypatch):
    with TestClient(build_app()) as c:
        as_people(monkeypatch)
        yield c


def H(name):
    return {**who(name), **ORIGIN}


def inbox(client, name):
    return client.get("/api/chat/notifications", headers=who(name)).json()["items"]


def test_channel_posts_threads_mentions_and_access(client):
    ch = client.post("/api/chat/spaces", headers=H("ana"), json={"name": "stock-desk", "people": ["ben@example.org"]}).json()
    sid = ch["id"]
    assert {m["email"] for m in ch["members"]} == {"ana@example.org", "ben@example.org"}
    post = client.post(f"/api/chat/spaces/{sid}/posts", headers=H("ana"),
                       json={"body": "Count aisle 4 today @ben@example.org"}).json()
    assert post["author"]["name"] == "Ana Example"
    notes = inbox(client, "ben")
    assert notes[0]["kind"] == "space.mention" and notes[0]["title"] == "#stock-desk" and notes[0]["link"] == f"/spaces/{sid}"
    reply = client.post(f"/api/chat/spaces/{sid}/posts", headers=H("ben"), json={"body": "On it", "parent_id": post["id"]}).json()
    assert reply["parent_id"] == post["id"]
    nested = client.post(f"/api/chat/spaces/{sid}/posts", headers=H("ben"), json={"body": "x", "parent_id": reply["id"]})
    assert nested.status_code == 422  # threads are one level
    edited = client.patch(f"/api/chat/posts/{reply['id']}", headers=H("ben"), json={"body": "On it now", "expected_revision": 1})
    assert edited.json()["edited"] and client.patch(f"/api/chat/posts/{reply['id']}", headers=H("ana"),
                                                    json={"body": "no", "expected_revision": 2}).status_code == 403
    r = client.post(f"/api/chat/posts/{post['id']}/reactions", headers=H("ben"), json={"emoji": "👍"}).json()
    assert r["reactions"] == [{"emoji": "👍", "count": 1, "mine": True}]
    client.post(f"/api/chat/posts/{post['id']}/pin", headers=H("ben"), json={"on": True})
    listed = client.get(f"/api/chat/spaces/{sid}/posts", headers=who("ana")).json()
    assert listed["pinned"][0]["id"] == post["id"] and listed["items"][0]["replies"] == 1
    client.delete(f"/api/chat/posts/{reply['id']}", headers=H("ben"))
    thread = client.get(f"/api/chat/spaces/{sid}/posts", params={"parent_id": post["id"]}, headers=who("ana")).json()
    assert thread["items"][0]["state"] == "deleted" and thread["items"][0]["body"] == ""
    # Someone outside a channel sees nothing, and is never notified about it.
    private = client.post("/api/chat/spaces", headers=H("ana"), json={"name": "ana-only"}).json()
    client.post(f"/api/chat/spaces/{private['id']}/posts", headers=H("ana"), json={"body": "@ben@example.org psst"})
    assert client.get(f"/api/chat/spaces/{private['id']}/posts", headers=who("ben")).status_code == 404
    assert len(inbox(client, "ben")) == 1
    client.post(f"/api/chat/spaces/{sid}/mute", headers=H("ben"), json={"muted": True})
    client.post(f"/api/chat/spaces/{sid}/posts", headers=H("ana"), json={"body": "@ben@example.org again"})
    assert len(inbox(client, "ben")) == 1  # muted: no new notice


def test_removed_member_loses_access_and_notices(client):
    ch = client.post("/api/chat/spaces", headers=H("ana"), json={"name": "ops", "people": ["ben@example.org"]}).json()
    client.post(f"/api/chat/spaces/{ch['id']}/posts", headers=H("ana"), json={"body": "@ben@example.org look"})
    assert len(inbox(client, "ben")) == 1
    client.put(f"/api/chat/spaces/{ch['id']}/members", headers=H("ana"), json={"people": [], "expected_revision": 1})
    assert client.get(f"/api/chat/spaces/{ch['id']}/posts", headers=who("ben")).status_code == 404
    assert inbox(client, "ben") == []  # the notice no longer shows


def test_direct_message_is_one_per_pair(client):
    a = client.post("/api/chat/spaces", headers=H("ana"), json={"kind": "dm", "people": ["ben@example.org"]}).json()
    b = client.post("/api/chat/spaces", headers=H("ben"), json={"kind": "dm", "people": ["ana@example.org"]}).json()
    assert a["id"] == b["id"] and a["kind"] == "dm"


def test_agent_answers_in_the_thread_on_behalf_of_the_asker(client):
    ch = client.post("/api/chat/spaces", headers=H("ana"), json={"name": "help"}).json()
    q = client.post(f"/api/chat/spaces/{ch['id']}/posts", headers=H("ana"), json={"body": "@agent what is a cycle count?"}).json()
    end = time.time() + 20
    while time.time() < end:
        thread = client.get(f"/api/chat/spaces/{ch['id']}/posts", params={"parent_id": q["id"]}, headers=who("ana")).json()
        if thread["items"] and thread["items"][0]["state"] != "pending":
            break
        time.sleep(0.2)
    reply = thread["items"][0]
    assert reply["author"]["kind"] == "agent" and reply["author"]["on_behalf_of"] == PEOPLE["ana"].id
    assert reply["state"] == "posted" and reply["body"]


def test_notes_refuse_stale_saves_and_keep_revisions(client):
    n = client.post("/api/chat/notes", headers=H("ana"), json={"title": "Recount plan", "body": "v1"}).json()
    first = client.put(f"/api/chat/notes/{n['id']}", headers=H("ana"), json={"title": "Recount plan", "body": "mine",
                                                                            "expected_revision": 1})
    assert first.status_code == 200
    stale = client.put(f"/api/chat/notes/{n['id']}", headers=H("ana"), json={"title": "Recount plan", "body": "other tab",
                                                                            "expected_revision": 1})
    assert stale.status_code == 409 and stale.json()["detail"]["current"]["body"] == "mine"
    revs = client.get(f"/api/chat/notes/{n['id']}/revisions", headers=who("ana")).json()["items"]
    assert [r["body"] for r in revs] == ["mine", "v1"]
    back = client.post(f"/api/chat/notes/{n['id']}/restore", headers=H("ana"), json={"revision": 1, "expected_revision": 2})
    assert back.json()["body"] == "v1" and back.json()["revision"] == 3
    assert client.get(f"/api/chat/notes", headers=who("ben")).json()["items"] == []  # personal notes are private


def test_calendar_reminders_deliver_once_and_follow_access(client):
    svc = client.app.state.chat.spaces
    start = time.time() + 600
    ev = client.post("/api/chat/calendar", headers=H("ana"), json={
        "title": "Stock take", "starts_at": start, "ends_at": start + 3600, "timezone": "Europe/London",
        "attendees": ["ben@example.org"], "reminder_minutes": 5}).json()
    listed = client.get("/api/chat/calendar", params={"start": start - 10, "end": start + 10}, headers=who("ben")).json()
    assert [e["title"] for e in listed["items"]] == ["Stock take"]
    assert svc.deliver_reminders(now=start - 3600) == 0  # not due yet
    assert svc.deliver_reminders(now=start - 250) == 2  # ana and ben, once each
    assert svc.deliver_reminders(now=start - 200) == 0
    assert inbox(client, "ben")[0]["kind"] == "calendar.reminder"
    # Deleting cancels what hasn't fired.
    ev2 = client.post("/api/chat/calendar", headers=H("ana"), json={
        "title": "Later", "starts_at": start + 7200, "ends_at": start + 9000, "timezone": "UTC", "reminder_minutes": 5}).json()
    assert client.get("/api/chat/calendar", params={"start": start, "end": start + 9000},
                      headers=who("ben")).json()["items"][0]["title"] == "Stock take"  # not "Later": not his
    client.delete(f"/api/chat/calendar/{ev2['id']}", headers=H("ana"))
    assert svc.deliver_reminders(now=start + 9000) == 0


def test_weekly_repeat_keeps_local_time_across_a_clock_change():
    from types import SimpleNamespace

    from hubzoid.chat.spaces import SpacesService

    tz = ZoneInfo("Europe/London")
    first = datetime(2026, 10, 19, 9, 0, tzinfo=tz).timestamp()  # BST
    e = SimpleNamespace(repeat="weekly", starts_at=first, timezone="Europe/London", repeat_until=None)
    occ = SpacesService.occurrences(e, first, first + 3 * 7 * 86400)
    assert [datetime.fromtimestamp(o, tz).hour for o in occ] == [9, 9, 9]  # 9:00 before and after 25 October
