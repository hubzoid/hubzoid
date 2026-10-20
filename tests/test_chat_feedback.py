"""Feedback, helpers, titles and notices (hubzoid.chat.feedback) through the API
on a scratch hub with the scripted runtime."""
from __future__ import annotations

import csv
import io
import json
import time

import pytest
from fastapi.testclient import TestClient

from hubzoid.chat import feedback as fb
from tests.chat_helpers import ORIGIN, as_people, build_app, make_hub, who
from tests.test_chat_operations import h, new_conv, op, send, snap, wait_run


@pytest.fixture
def hub(tmp_path, monkeypatch):
    return make_hub(tmp_path, monkeypatch)


@pytest.fixture
def client(hub):
    with TestClient(build_app()) as c:
        yield c


def answered(client, text="hello"):
    cid, b = new_conv(client)
    out = send(client, cid, text, b)
    wait_run(client, out["run_id"])
    return cid, out


def test_feedback_binds_author_and_exact_message(client, monkeypatch):
    cid, out = answered(client)
    url = f"/api/conversations/{cid}/messages/{out['assistant_message_id']}/feedback"
    r = client.put(url, headers=ORIGIN, json={"expected_revision": "absent", "rating": "negative",
                                             "reason_ids": ["missing_source"], "comment": "Please cite policy",
                                             "score": 2})
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 1 and r.json()["author_id"]
    again = client.put(url, headers=ORIGIN, json={"expected_revision": "absent", "rating": "positive"})
    assert again.status_code == 409
    bad = client.put(url, headers=ORIGIN, json={"expected_revision": 1, "rating": "positive", "reason_ids": ["nope"]})
    assert bad.status_code == 422
    user_msg = client.put(f"/api/conversations/{cid}/messages/{out['user_message_id']}/feedback", headers=ORIGIN,
                          json={"rating": "positive"})
    assert user_msg.status_code == 422
    assert client.get(url).json()["feedback"]["comment"] == "Please cite policy"
    assert client.request("DELETE", url, headers=ORIGIN, json={"expected_revision": 1}).status_code == 204
    assert client.request("DELETE", url, headers=ORIGIN, json={"expected_revision": 1}).status_code == 204
    assert client.get(url).json()["feedback"] is None


def test_feedback_list_needs_manager_and_csv_is_safe(client, hub, monkeypatch):
    cid, out = answered(client)
    client.put(f"/api/conversations/{cid}/messages/{out['assistant_message_id']}/feedback", headers=ORIGIN,
               json={"rating": "negative", "comment": "=HYPERLINK(\"x\")"})
    listed = client.get("/api/chat/feedback").json()
    assert len(listed["items"]) == 1
    data = client.get("/api/chat/feedback/export?format=csv").text
    rows = list(csv.reader(io.StringIO(data)))
    assert rows[1][rows[0].index("comment")].startswith("'=")
    as_people(monkeypatch)
    assert client.get("/api/chat/feedback", headers=who("ben")).status_code == 403
    # A manager sees the rating, but not a comment quoting a conversation they can't open.
    from hubzoid.access import store_for

    store_for(hub).grant("ben@example.org", hub.name, "manage_access", actor="t")
    item = client.get("/api/chat/feedback", headers=who("ben")).json()["items"][0]
    assert item["rating"] == "negative" and item["comment"] is None and item["comment_hidden"]
    assert "HYPERLINK" not in client.get("/api/chat/feedback/export?format=csv", headers=who("ben")).text


def test_helper_followups_native_structured_and_stale_check(client, hub, monkeypatch):
    # Follow-ups are off by default; the deployment turns them on.
    from hubzoid.chat import config as cfg

    svc = cfg.service_for(hub)
    doc = svc.active().document
    doc["helpers"]["enabled"]["followups"] = True
    svc.apply(doc, expected_revision=None, actor="t")
    cid, out = answered(client)
    b = next(x for x in snap(client, cid)["branches"] if x["id"] == out["branch_id"])
    calls = []

    def fake_complete(hub_dir, spec, **kw):
        calls.append(spec)
        return {"json": {"items": [{"title": "Next", "prompt": "What next?"}]}, "structured": "native",
                "schema_errors": [], "model": "fake"}

    service = client.app.state.chat.feedback
    job = service.start_helper({"id": cid}, snap(client, cid)["conversation"]["owner_id"], "followups", b["id"],
                               b["revision"])
    done = service.run_helper(job["id"], complete=fake_complete)
    assert done["state"] == "complete" and done["result"]["items"][0]["title"] == "Next"
    assert calls[0]["response_format"] == "json" and calls[0]["schema"]["required"] == ["items"]
    assert done["evidence"]["structured"] == "native"
    # The branch moved while a second helper ran: its result is not published.
    job2 = service.start_helper({"id": cid}, snap(client, cid)["conversation"]["owner_id"], "followups", b["id"],
                                b["revision"])
    assert job2["id"] == job["id"]  # same input and settings: the same job
    stale = client.post("/api/chat/helpers", headers=ORIGIN, json={"kind": "followups", "conversation_id": cid,
                                                                   "branch_id": b["id"], "input_revision": 99})
    assert stale.status_code == 409


def test_typed_title_wins_over_late_helper(client, hub):
    cid, out = answered(client)
    assert client.patch(f"/api/conversations/{cid}/title", headers=ORIGIN, json={"title": "Stock policy"}).json()[
        "source"] == "user"
    b = next(x for x in snap(client, cid)["branches"] if x["id"] == out["branch_id"])
    service = client.app.state.chat.feedback
    owner = snap(client, cid)["conversation"]["owner_id"]
    job = service.start_helper({"id": cid}, owner, "titles", b["id"], b["revision"])
    done = service.run_helper(job["id"], complete=lambda *a, **k: {"json": {"title": "Generated"},
                                                                   "schema_errors": [], "structured": "native"})
    assert done["state"] == "stale" and done["error_code"] == "title_set_by_person"
    assert snap(client, cid)["conversation"]["title"] == "Stock policy"


def test_run_end_notifies_once_and_not_while_looking(client):
    cid, out = answered(client)
    items = client.get("/api/chat/notifications").json()["items"]
    assert [i["kind"] for i in items] == ["run.completed"] and items[0]["conversation_id"] == cid
    assert items[0]["hub"], "a notice names its hub, so the Inbox can filter by agent"
    # Looking at the conversation (delivery: away): no notice for the next reply.
    client.post("/api/chat/notification-presence", headers=ORIGIN,
                json={"device_id": "d1", "conversation_id": cid, "focused": True})
    b = next(x for x in snap(client, cid)["branches"] if x["id"] == out["branch_id"])
    wait_run(client, send(client, cid, "again", b, parent=out["assistant_message_id"])["run_id"])
    assert len(client.get("/api/chat/notifications").json()["items"]) == 1
    nid = items[0]["id"]
    assert client.patch(f"/api/chat/notifications/{nid}", headers=ORIGIN, json={"state": "read"}).json()["state"] == "read"
    assert client.get("/api/chat/notifications").json()["unread"] == 0


def test_stopped_reply_notifies_nobody(client):
    cid, b = new_conv(client)
    first = send(client, cid, "slow please", b)
    time.sleep(0.4)
    client.post(f"/api/runs/{first['run_id']}/cancel", headers=ORIGIN, json={})
    wait_run(client, first["run_id"])
    assert client.get("/api/chat/notifications").json()["items"] == []


def test_targets_need_verification_and_safe_urls(client, monkeypatch):
    base = {"type": "webhook", "recipient_ref": "https://hooks.example.com/hz", "connection_ref": "env:HZ_HOOK",
            "events": ["run.completed"], "expected_revision": "absent"}
    monkeypatch.setattr(fb.socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("93.184.216.34", 443))])
    on = client.put("/api/chat/notification-targets/ops?scope=own", headers=ORIGIN, json={**base, "enabled": True})
    assert on.status_code == 422 and on.json()["detail"]["code"] == "target_unverified"
    made = client.put("/api/chat/notification-targets/ops?scope=own", headers=ORIGIN, json=base)
    assert made.status_code == 200 and made.json()["verified"] is False
    plain = client.put("/api/chat/notification-targets/x?scope=own", headers=ORIGIN,
                       json={**base, "connection_ref": "sk-live-token"})
    assert plain.status_code == 422
    monkeypatch.setattr(fb.socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("10.0.0.5", 443))])
    private = client.put("/api/chat/notification-targets/y?scope=own", headers=ORIGIN, json=base)
    assert private.status_code == 422
    assert client.put("/api/chat/notification-targets/z?scope=hub", headers=ORIGIN, json=base).status_code in (200, 422)


def test_webhook_delivery_signed_and_ambiguous_not_resent(client, hub, monkeypatch):
    monkeypatch.setenv("HZ_HOOK", "s3cret")
    monkeypatch.setattr(fb.socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("93.184.216.34", 443))])
    service = client.app.state.chat.feedback
    owner = snap(client, new_conv(client)[0])["conversation"]["owner_id"]
    scope = service.scope_of("own", owner, manager=False)
    service.put_target(scope, "ops", {"type": "webhook", "recipient_ref": "https://hooks.example.com/hz",
                                      "connection_ref": "env:HZ_HOOK", "events": ["run.completed"]},
                       owner_id=owner, expected_revision="absent")
    seen = []

    def ok_send(target, payload):
        seen.append(payload)
        return True, "http_200"

    service.verify_target(scope, "ops", send=ok_send)
    service.put_target(scope, "ops", {"type": "webhook", "recipient_ref": "https://hooks.example.com/hz",
                                      "connection_ref": "env:HZ_HOOK", "events": ["run.completed"],
                                      "enabled": True}, owner_id=owner, expected_revision=2)
    monkeypatch.setattr(client.app.state.chat.engine, "on_terminal", None)  # deliver by hand below
    service.on_run_terminal(run_id="r1", conversation_id="c", branch_id="b", message_id="m", actor_id=owner,
                            status="complete", stopped_by_self=False)

    def timeout_send(target, payload):
        raise TimeoutError

    service.deliver_due(send=timeout_send)
    hist = service.delivery_history(scope, "ops")
    assert hist[0]["state"] == "ambiguous"
    assert service.deliver_due(send=ok_send, now=time.time() + 3600) == 0  # never resent automatically
    # A delivery waits for the target exactly as approved: change the target and it is suppressed.
    service.on_run_terminal(run_id="r2", conversation_id="c", branch_id="b", message_id="m2", actor_id=owner,
                            status="complete", stopped_by_self=False)
    service.put_target(scope, "ops", {"type": "webhook", "recipient_ref": "https://other.example.com/hz",
                                      "connection_ref": "env:HZ_HOOK", "events": ["run.completed"]},
                       owner_id=owner, expected_revision=3)
    sent = len(seen)
    service.deliver_due(send=ok_send, now=time.time() + 3600)
    assert len(seen) == sent and service.delivery_history(scope, "ops")[0]["state"] == "suppressed"
    # The checked address is the one connected to; the name stays for Host and TLS.
    url, host, ext = fb.pinned_request("https://hooks.example.com:8443/hz?a=1", "93.184.216.34")
    assert url == "https://93.184.216.34:8443/hz?a=1" and host == {"host": "hooks.example.com:8443"}
    assert ext == {"sni_hostname": "hooks.example.com"}
    assert fb.sign(b"k", "1", b"{}") == fb.sign(b"k", "1", b"{}") and fb.sign(b"k", "1", b"{}").startswith("v1=")
    assert json.loads(json.dumps(seen[0]))["type"] == "hubzoid.verification"


def test_personal_notification_preferences_follow_policy(client, hub):
    from hubzoid.chat import config as cfg

    svc = cfg.service_for(hub)
    owner = snap(client, new_conv(client)[0])["conversation"]["owner_id"]
    out = svc.set_preferences(owner, {"notifications.desktop": True, "mode": "dark"}, expected_revision=0)
    assert out["values"]["notifications.desktop"] is True
    with pytest.raises(cfg.ConfigError) as exc:
        svc.set_preferences(owner, {"helpers.enabled.followups": True}, expected_revision=1)
    assert exc.value.code == "policy_locked"
