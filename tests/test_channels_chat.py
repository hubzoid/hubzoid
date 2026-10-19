"""A channel thread answered by the native chat engine, through the real
bridge app with the scripted test runtime: signed delivery in, one run, one
reply in the same thread, and recovery when the bridge restarts."""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from hubzoid.channels import admission, config, links, registry, store

from tests.channel_fake import FakeAdapter, delivery, headers
from tests.chat_helpers import build_app, make_hub

SECRET = "fake-signing-value"   # gitleaks:allow (a test value)

pytestmark = pytest.mark.slow


@pytest.fixture
def fake():
    adapter = FakeAdapter()
    registry.register(adapter)
    yield adapter
    registry.unregister("fake")


@pytest.fixture
def hub(tmp_path, monkeypatch, fake):
    hub = make_hub(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_CHANNEL_SECRET", SECRET)
    (hub / "identity").mkdir(exist_ok=True)
    (hub / "identity" / "access.csv").write_text("phone,email,groups\n,ana@example.org,support\n"
                                                 ",ben@example.org,support\n")
    from hubzoid.auth import users

    from tests.chat_helpers import PEOPLE

    for person in PEOPLE.values():   # the same ids the chat app's test sign-in uses
        if users.find_by_email(hub, person.email) is None:
            users.create(hub, email=person.email, name=person.name, id=person.id)
    data = config.load_text(hub, config.render({
        "version": 1, "id": "team", "provider": "fake", "enabled": True, "agent": "main",
        "allowed_groups": ["support"], "provider_config": {"secret_ref": "env:FAKE_CHANNEL_SECRET"}})).data
    config.apply(hub, data, expected_revision="absent", actor="test")
    conn = config.active(hub, "team")
    links.link(conn, external_id="U1", account="ana@example.org", actor="test")
    links.link(conn, external_id="U2", account="ben@example.org", actor="test")
    return hub


def post(client, hub, *events, delivery_id="d1"):
    raw, did = delivery(*events, delivery_id=delivery_id)
    hub_id = config.hub_of(hub)
    return client.post(f"/api/hubs/{hub_id}/channels/team/webhook", content=raw,
                       headers={**headers(SECRET, raw, did), "content-type": "application/json"})


def wait_for(predicate, timeout=30.0):
    end = time.time() + timeout
    while time.time() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("timed out")


def results(fake):
    return [s for s in fake.sent if s["kind"] == "result"]


def test_mention_gets_one_reply_in_its_thread(hub, fake):
    with TestClient(build_app()) as client:
        r = post(client, hub, {"id": "e1", "thread": "t1", "user": "U1", "text": "What is two plus two?"})
        assert r.status_code == 200 and r.json()["status"] == "accepted"
        assert post(client, hub, {"id": "e1", "thread": "t1", "user": "U1",
                                  "text": "What is two plus two?"}).json()["status"] == "duplicate"
        wait_for(lambda: results(fake))
        time.sleep(1.0)
    sent = fake.sent
    assert [s["kind"] for s in sent] == ["ack", "result"]
    assert all(s["thread"] == "t1" for s in sent)
    row = store.list_deliveries(hub, config.hub_of(hub), "team")["items"][0]
    conv = row["route_json"]["conversation_id"]
    thread = store.get_thread(hub, row["thread_id"])
    assert thread["subscribed"] == 1 and list(thread["conversations_json"].values()) == [conv]


def test_follow_up_continues_the_same_conversation(hub, fake):
    with TestClient(build_app()) as client:
        post(client, hub, {"id": "e1", "thread": "t1", "user": "U1", "text": "First question"})
        wait_for(lambda: len(results(fake)) == 1)
        # A plain message in the subscribed thread, no mention needed.
        post(client, hub, {"id": "e2", "thread": "t1", "user": "U1", "text": "And a follow-up",
                           "trigger": False}, delivery_id="d2")
        wait_for(lambda: len(results(fake)) == 2)
    rows = store.list_deliveries(hub, config.hub_of(hub), "team")["items"]
    assert {r["route_json"]["conversation_id"] for r in rows} == {rows[0]["route_json"]["conversation_id"]}


def test_two_people_get_their_own_conversations(hub, fake):
    with TestClient(build_app()) as client:
        post(client, hub, {"id": "e1", "thread": "t1", "user": "U1", "text": "Ana asks"})
        post(client, hub, {"id": "e2", "thread": "t1", "user": "U2", "text": "Ben asks"}, delivery_id="d2")
        wait_for(lambda: len(results(fake)) == 2, timeout=45)
    rows = store.list_deliveries(hub, config.hub_of(hub), "team")["items"]
    convs = {r["route_json"]["account"]: r["route_json"]["conversation_id"] for r in rows}
    assert set(convs) == {"ana@example.org", "ben@example.org"} and len(set(convs.values())) == 2


def test_delivery_saved_before_a_restart_is_answered_after(hub, fake):
    conn = config.active(hub, "team")
    raw, did = delivery({"id": "e1", "thread": "t1", "user": "U1", "text": "Saved, then the bridge stopped"})
    out = admission.receive(hub, "team", raw, headers(SECRET, raw, did), {})
    assert out.status == "accepted" and conn.enabled
    assert fake.sent == []
    with TestClient(build_app()):
        wait_for(lambda: results(fake))
    assert [s["kind"] for s in fake.sent] == ["ack", "result"]


def test_finished_reply_not_posted_before_a_restart_is_posted_once(hub, fake, monkeypatch):
    from hubzoid.channels import router

    real = router.deliver_results

    async def crashed(*a, **k):     # the bridge stops between "reply done" and "posted"
        with router._state.lock:
            router._state.results.clear()
        return 0

    monkeypatch.setattr(router, "deliver_results", crashed)
    with TestClient(build_app()) as client:
        post(client, hub, {"id": "e1", "thread": "t1", "user": "U1", "text": "Answer me"})
        hub_id = config.hub_of(hub)
        rows = lambda: store.list_deliveries(hub, hub_id, "team")["items"]   # noqa: E731
        wait_for(lambda: rows()[0]["status"] == "routed")
        conv = rows()[0]["route_json"]["conversation_id"]
        chat = client.app.state.chat

        def finished():
            from hubzoid.chat.store import messages
            import sqlalchemy as sa

            with chat.engine.engine.connect() as c:
                return c.execute(sa.select(messages.c.id).where(
                    messages.c.conversation_id == conv, messages.c.role == "assistant",
                    messages.c.status == "complete")).first()

        wait_for(finished)
    assert results(fake) == []
    monkeypatch.setattr(router, "deliver_results", real)
    with TestClient(build_app()):
        wait_for(lambda: results(fake))
    with TestClient(build_app()):
        time.sleep(2.0)
    assert len(results(fake)) == 1


def test_own_chat_app_message_is_not_posted_to_the_thread(hub, fake, monkeypatch):
    """The person opens the thread's conversation in the chat app and asks
    something there: that answer stays in the chat app."""
    import uuid

    from tests.chat_helpers import as_people, who

    as_people(monkeypatch)
    with TestClient(build_app()) as client:
        post(client, hub, {"id": "e1", "thread": "t1", "user": "U1", "text": "From Slack"})
        wait_for(lambda: len(results(fake)) == 1)
        row = store.list_deliveries(hub, config.hub_of(hub), "team")["items"][0]
        conv = row["route_json"]["conversation_id"]
        branches = client.get(f"/api/conversations/{conv}/branches", headers=who("ana")).json()["items"]
        b = branches[0]
        r = client.post(f"/api/conversations/{conv}/operations",
                        headers={**who("ana"), "Idempotency-Key": str(uuid.uuid4())},
                        json={"kind": "send", "branch_id": b["id"], "expected_revision": b["revision"],
                              "parent_id": b["head_id"], "content": [{"type": "text", "text": "Private question"}]})
        assert r.status_code == 202, r.text
        run_id = r.json()["run_id"]
        wait_for(lambda: client.get(f"/api/chat/runs/{run_id}", headers=who("ana")).json()["status"]
                 in ("complete", "failed", "stopped"))
        time.sleep(2.0)
    assert len(results(fake)) == 1
