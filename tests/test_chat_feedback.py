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
from tests.test_chat_operations import h, new_conv, op, send, snap, wait_run  # noqa: F401


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
    __import__('tests.chat_helpers', fromlist=['x']).write_chat_config(svc.hub_dir, doc)
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


# -- feedback across surfaces (proposals/2026-10-20-feedback-across-channels.md) -------------------

def set_feedback(hub, **values):
    from hubzoid.chat import config as cfg

    svc = cfg.service_for(hub)
    active = svc.active()
    doc = active.document
    doc["feedback"].update(values)
    svc.apply(doc, expected_revision=active.revision or None, actor="t")


def fb_url(cid, mid):
    return f"/api/conversations/{cid}/messages/{mid}/feedback"


def test_reload_shows_mine_and_change_or_remove_keeps_revisions(client):
    cid, out = answered(client)
    mid = out["assistant_message_id"]
    saved = client.put(fb_url(cid, mid), headers=ORIGIN, json={
        "expected_revision": "absent", "rating": "negative", "reason_ids": ["missing_source"],
        "comment": "Cite the policy"}).json()
    # A reload asks once for the whole conversation and gets the reason and comment back.
    mine = client.get(f"/api/conversations/{cid}/feedback").json()["items"]
    assert [(m["message_id"], m["rating"], m["reason_ids"], m["comment"]) for m in mine] == [
        (mid, "negative", ["missing_source"], "Cite the policy")]
    assert mine[0]["source"] == "web" and mine[0]["target_kind"] == "message"
    # Another window saved first: an edit made from the old revision never overwrites it.
    client.put(fb_url(cid, mid), headers=ORIGIN, json={"expected_revision": saved["revision"], "rating": "positive"})
    stale = client.put(fb_url(cid, mid), headers=ORIGIN, json={"expected_revision": saved["revision"],
                                                               "rating": "negative", "comment": "draft"})
    assert stale.status_code == 409 and stale.json()["detail"]["current_revision"] == saved["revision"] + 1
    assert client.get(fb_url(cid, mid)).json()["feedback"]["rating"] == "positive"
    gone = client.request("DELETE", fb_url(cid, mid), headers=ORIGIN, json={"expected_revision": saved["revision"]})
    assert gone.status_code == 409    # removing from an old revision is refused too
    assert client.request("DELETE", fb_url(cid, mid), headers=ORIGIN,
                          json={"expected_revision": saved["revision"] + 1}).status_code == 204
    assert client.get(f"/api/conversations/{cid}/feedback").json()["items"] == []
    again = client.put(fb_url(cid, mid), headers=ORIGIN, json={"expected_revision": "absent", "rating": "positive"})
    assert again.status_code == 200 and again.json()["revision"] == saved["revision"] + 3


def test_off_refuses_every_new_rating_but_not_removal(client, hub):
    cid, out = answered(client)
    mid = out["assistant_message_id"]
    client.put(fb_url(cid, mid), headers=ORIGIN, json={"rating": "positive"})
    set_feedback(hub, enabled=False)
    assert client.get("/api/chat/capabilities").json()["features"]["feedback"] is False
    refused = client.put(fb_url(cid, mid), headers=ORIGIN, json={"expected_revision": 1, "rating": "negative"})
    assert refused.status_code == 403 and refused.json()["detail"]["code"] == "feature_disabled"
    svc = client.app.state.chat.feedback
    from hubzoid.chat.graph import GraphError

    with pytest.raises(GraphError):    # the tool and channel path too
        svc.record(target_kind="message", target_id=mid, author_id="u_other", source="slack", rating="positive")
    assert client.request("DELETE", fb_url(cid, mid), headers=ORIGIN, json={}).status_code == 204


def test_unfinished_and_temporary_answers_take_no_rating(client):
    cid, b = new_conv(client)
    slow = send(client, cid, "slow please", b)
    early = client.put(fb_url(cid, slow["assistant_message_id"]), headers=ORIGIN, json={"rating": "positive"})
    assert early.status_code == 409 and early.json()["detail"]["code"] == "message_not_terminal"
    wait_run(client, slow["run_id"])
    agent = client.app.state.chat.model_label
    tmp = client.post("/api/conversations", headers=ORIGIN, json={"agent": agent, "temporary": True}).json()
    out = send(client, tmp["conversation"]["id"], "hello", tmp["branch"])
    wait_run(client, out["run_id"])
    r = client.put(fb_url(tmp["conversation"]["id"], out["assistant_message_id"]), headers=ORIGIN,
                   json={"rating": "positive"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "temporary_resource"


def test_review_list_filters_counts_and_hides_what_the_reviewer_cannot_open(client, hub, monkeypatch):
    from hubzoid.access import store_for
    from hubzoid.auth import users
    from tests.chat_helpers import PEOPLE

    as_people(monkeypatch)
    for person in PEOPLE.values():   # real accounts, so the review shows who rated
        if users.find_by_email(hub, person.email) is None:
            users.create(hub, email=person.email, name=person.name, id=person.id)
    rated = {}
    for name, rating, comment in (("ana", "negative", "Price is =SUM(A1) wrong"), ("ben", "positive", None)):
        cid, b = new_conv(client, who(name))
        out = send(client, cid, f"stock question from {name}", b, who(name))
        wait_run(client, out["run_id"], who(name))
        body = {"rating": rating, **({"comment": comment, "reason_ids": ["incorrect"]} if comment else {})}
        assert client.put(fb_url(cid, out["assistant_message_id"]), headers=who(name), json=body).status_code == 200
        rated[name] = cid
    assert client.get("/api/chat/feedback", headers=who("ben")).status_code == 403
    store_for(hub).grant("ben@example.org", hub.name, "manage_access", actor="t")
    hub_id = hub.name.lower()
    page = client.get(f"/api/hubs/{hub_id}/feedback", headers=who("ben")).json()
    assert page["counts"] == {"positive": 1, "negative": 1}
    by = {i["rating"]: i for i in page["items"]}
    # Ben reviews Ana's rating but cannot open her conversation: no comment, words, answer or link.
    assert by["negative"]["reason_ids"] == ["incorrect"] and by["negative"]["comment"] is None
    assert by["negative"]["comment_hidden"] and by["negative"]["source_hidden"]
    assert by["negative"]["excerpt"] is None and by["negative"]["context"] is None
    assert by["negative"]["author"] == "Ana Example"
    # His own conversation shows the answer and where it is.
    assert "stock question from ben" in by["positive"]["excerpt"]
    assert by["positive"]["context"] == {"kind": "conversation", "conversation_id": rated["ben"]}
    only = client.get(f"/api/hubs/{hub_id}/feedback?rating=negative&source=web&reason=incorrect",
                      headers=who("ben")).json()
    assert [i["rating"] for i in only["items"]] == ["negative"] and only["counts"]["positive"] == 0
    assert client.get(f"/api/hubs/{hub_id}/feedback?source=slack", headers=who("ben")).json()["items"] == []
    assert client.get(f"/api/hubs/{hub_id}/feedback?source=email", headers=who("ben")).status_code == 400
    assert client.get("/api/hubs/another-hub/feedback", headers=who("ben")).status_code == 404
    first = client.get(f"/api/hubs/{hub_id}/feedback?limit=1", headers=who("ben")).json()
    second = client.get(f"/api/hubs/{hub_id}/feedback?limit=1&cursor={first['next_cursor']}",
                        headers=who("ben")).json()
    assert {first["items"][0]["id"], second["items"][0]["id"]} == {i["id"] for i in page["items"]}
    # Export: the same rules; the 1.x columns first and in order, the new ones after.
    rows = list(csv.reader(io.StringIO(client.get(f"/api/hubs/{hub_id}/feedback/export?format=csv",
                                                  headers=who("ben")).text)))
    assert rows[0][:11] == ["id", "conversation_id", "message_id", "author_id", "agent_id", "rating", "reason_ids",
                            "score", "tags", "comment", "updated_at"]
    assert rows[0][11:] == ["source", "target_kind", "author_kind", "author", "channel_id", "excerpt"]
    assert "SUM" not in "".join(sum(rows, []))
    assert client.get("/api/chat/feedback/export?format=csv", headers=who("ana")).status_code == 403
    exported = json.loads(client.get(f"/api/hubs/{hub_id}/feedback/export?format=json", headers=who("ben")).text)
    assert {i["id"] for i in exported["items"]} == {i["id"] for i in page["items"]}


def test_safe_csv_neutralises_formulas_in_visible_comments(client, hub):
    cid, out = answered(client)
    client.put(fb_url(cid, out["assistant_message_id"]), headers=ORIGIN,
               json={"rating": "negative", "comment": "@cmd|' /C calc'!A0"})
    rows = list(csv.reader(io.StringIO(client.get("/api/chat/feedback/export?format=csv").text)))
    assert rows[1][rows[0].index("comment")].startswith("'@")


def test_deleting_a_conversation_takes_its_ratings(client):
    import sqlalchemy as sa

    cid, out = answered(client)
    client.put(fb_url(cid, out["assistant_message_id"]), headers=ORIGIN,
               json={"rating": "negative", "comment": "quotes the private chat"})
    conv = snap(client, cid)["conversation"]
    r = client.request("DELETE", f"/api/conversations/{cid}", headers=h(),
                       json={"expected_revision": conv.get("graph_revision")})
    assert r.status_code in (200, 202, 204), r.text
    assert client.get("/api/chat/feedback").json()["items"] == []
    with client.app.state.chat.engine.engine.connect() as c:
        row = c.execute(sa.select(fb.feedback).where(fb.feedback.c.conversation_id == cid)).first()
    assert row.deleted_at is not None and row.comment is None


def test_rows_from_before_the_migration_read_as_web_ratings(client):
    import sqlalchemy as sa

    cid, out = answered(client)
    owner = snap(client, cid)["conversation"]["owner_id"]
    with client.app.state.chat.engine.engine.begin() as c:   # a 1.x row: none of the op_0072 columns set
        c.execute(sa.text("INSERT INTO hz_feedback (id, hub_id, conversation_id, branch_id, message_id, author_id, "
                          "agent_id, rating, reason_ids, comment, tags, score, revision, source_metadata, created_at, "
                          "updated_at) VALUES ('old-1', :hub, :cid, NULL, :mid, :owner, 'a', 'positive', '[]', NULL, "
                          "'[]', 4, 1, NULL, 1, 1)"),
                  {"hub": client.app.state.chat.hub, "cid": cid, "mid": out["assistant_message_id"], "owner": owner})
    item = client.get("/api/chat/feedback").json()["items"][0]
    assert (item["source"], item["target_kind"], item["author_kind"], item["score"]) == ("web", "message",
                                                                                          "account", 4)
    mine = client.get(fb_url(cid, out["assistant_message_id"])).json()["feedback"]
    assert mine["id"] == "old-1"
    edited = client.put(fb_url(cid, out["assistant_message_id"]), headers=ORIGIN,
                        json={"expected_revision": 1, "rating": "negative"})
    assert edited.status_code == 200 and edited.json()["revision"] == 2
