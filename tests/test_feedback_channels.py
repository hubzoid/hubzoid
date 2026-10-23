"""Ratings from Slack and WhatsApp (hubzoid.channels.feedback) with fake
providers: signed requests in, the real admission, router and feedback
service, nothing sent to a real provider.

Slack runs through the real bridge (an agent answers with the scripted
runtime), so those tests are slow. WhatsApp routes deliveries directly."""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from hubzoid.channels import config, links, store
from hubzoid.channels import feedback as cfb
from hubzoid.chat import feedback as fb
from tests import test_channels_whatsapp as wa_t
from tests.chat_helpers import build_app, make_hub, write_chat_config
from tests.test_channels_slack import SIGNING, FakeWeb, signed
from tests.test_channels_whatsapp import (APP_SECRET, SENDER, _mode, _post, _released, hub_dir, meta,  # noqa: F401
                                          payload, post, route_all, started, text_msg)


def live(engine) -> list[dict]:
    with engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(sa.select(fb.feedback).where(fb.feedback.c.deleted_at.is_(None)))]


def wait_for(predicate, timeout=30.0):
    end = time.time() + timeout
    while time.time() < end:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("timed out")


# -- Slack ------------------------------------------------------------------------------------

class Slack(FakeWeb):
    def __init__(self):
        super().__init__()
        self.ephemeral: list[dict] = []
        self.views: list[dict] = []

    def chat_postEphemeral(self, **kw):
        self.ephemeral.append(kw)
        return {"ok": True}

    def views_open(self, **kw):
        self.views.append(kw)
        return {"ok": True}


@pytest.fixture
def slack_web(monkeypatch):
    from hubzoid.channels.slack import adapter as slack

    fake = Slack()
    monkeypatch.setattr(slack, "client", lambda conn, writes=False: fake)
    return fake


@pytest.fixture
def slack_hub(tmp_path, monkeypatch, slack_web):
    hub = make_hub(tmp_path, monkeypatch)
    monkeypatch.setenv("SLACK_TEST_SIGNING", SIGNING)
    monkeypatch.setenv("SLACK_TEST_BOT", "xoxb-test-value")    # gitleaks:allow (a test value)
    from hubzoid.auth import users
    from tests.chat_helpers import PEOPLE

    from hubzoid.access import store_for

    for person in PEOPLE.values():
        store_for(hub).grant(person.email, hub.name, "channel_slack-main", actor="test")
        if users.find_by_email(hub, person.email) is None:
            users.create(hub, email=person.email, name=person.name, id=person.id)
    data = config.load_text(hub, config.render({
        "version": 1, "id": "slack-main", "provider": "slack", "enabled": True, "agent": "main",
        "allowed_groups": ["support"], "feedback": True,
        "provider_config": {"transport": "http", "installation_team_id": "T0TEAM", "app_id": "A0APP",
                            "bot_token_ref": "env:SLACK_TEST_BOT", "signing_secret_ref": "env:SLACK_TEST_SIGNING",
                            "allowed_channel_ids": ["C0OPS"]}})).data
    config.apply(hub, data, expected_revision="absent", actor="test")
    conn = config.active(hub, "slack-main")
    links.link(conn, external_id="U0ANA", account="ana@example.org", actor="test")
    links.link(conn, external_id="U0BEN", account="ben@example.org", actor="test")
    return hub


def webhook(client, hub, raw: bytes, headers: dict):
    return client.post(f"/api/hubs/{config.hub_of(hub)}/channels/slack-main/webhook", content=raw, headers=headers)


def mention(client, hub, text="<@UBOT> what is the price of SKU 42?", ts="1700000000.000100", event_id="Ev1"):
    body = {"type": "event_callback", "api_app_id": "A0APP", "team_id": "T0TEAM", "event_id": event_id,
            "event": {"type": "app_mention", "channel": "C0OPS", "ts": ts, "user": "U0ANA", "text": text}}
    raw, headers = signed(body)
    return webhook(client, hub, raw, headers)


def click(prompt: dict, value="up", user="U0ANA", trigger="trig-1", action_ts="1700000100.1", block=None):
    """Slack's block_actions payload for a click on the rating buttons (form-encoded, as Slack sends it)."""
    payload = {"type": "block_actions", "api_app_id": "A0APP", "team": {"id": "T0TEAM"}, "trigger_id": trigger,
               "user": {"id": user, "team_id": "T0TEAM"}, "channel": {"id": prompt["channel"]},
               "container": {"type": "message", "message_ts": prompt["ts"], "channel_id": prompt["channel"],
                             "thread_ts": prompt["thread_ts"]},
               "message": {"ts": prompt["ts"], "thread_ts": prompt["thread_ts"]},
               "actions": [{"type": "feedback_buttons", "action_id": cfb.ACTION_ID, "value": value,
                            "block_id": block or prompt["blocks"][0]["block_id"], "action_ts": action_ts}]}
    raw = urlencode({"payload": json.dumps(payload)}).encode()
    from slack_sdk.signature import SignatureVerifier

    ts = str(int(time.time()))
    return raw, {"x-slack-request-timestamp": ts, "content-type": "application/x-www-form-urlencoded",
                 "x-slack-signature": SignatureVerifier(SIGNING).generate_signature(timestamp=ts, body=raw)}


def submit(view: dict, reasons=("missing_source",), comment="Cite the price list", user="U0ANA"):
    state = {"reasons": {"value": {"selected_options": [{"value": r} for r in reasons]}},
             "comment": {"value": {"value": comment}}}
    payload = {"type": "view_submission", "api_app_id": "A0APP", "team": {"id": "T0TEAM"},
               "trigger_id": f"sub-{time.time()}", "user": {"id": user, "team_id": "T0TEAM"},
               "view": {"id": "V1", "callback_id": view["callback_id"], "private_metadata": view["private_metadata"],
                        "state": {"values": state}}}
    raw = urlencode({"payload": json.dumps(payload)}).encode()
    from slack_sdk.signature import SignatureVerifier

    ts = str(int(time.time()))
    return raw, {"x-slack-request-timestamp": ts, "content-type": "application/x-www-form-urlencoded",
                 "x-slack-signature": SignatureVerifier(SIGNING).generate_signature(timestamp=ts, body=raw)}


def prompt_of(web: Slack) -> dict:
    return next(p for p in web.posts if p.get("blocks"))


@pytest.mark.slow
def test_slack_rating_end_to_end(slack_hub, slack_web, monkeypatch):
    hub_id = config.hub_of(slack_hub)
    with TestClient(build_app()) as client:
        assert mention(client, slack_hub).json()["status"] == "accepted"
        prompt = wait_for(lambda: next((p for p in slack_web.posts if p.get("blocks")), None))
        kinds = [p.get("blocks") is not None for p in slack_web.posts]
        assert kinds == [False, False, True]     # the acknowledgement and the answer carry no buttons
        assert prompt["blocks"][0]["type"] == "context_actions" and prompt["thread_ts"] == "1700000000.000100"
        engine = client.app.state.chat.engine.engine
        # Helpful, delivered twice by Slack (same bytes): one rating, one private confirmation.
        raw, headers = click(prompt)
        assert webhook(client, slack_hub, raw, headers).text == ""
        webhook(client, slack_hub, raw, headers)
        [row] = wait_for(lambda: live(engine))
        answer = next(p for p in slack_web.posts if p["text"].startswith("You said") or "scripted" in p["text"])
        assert (row["source"], row["author_id"], row["rating"], row["channel_id"]) == ("slack", "u_ana", "positive",
                                                                                       "slack-main")
        msg = client.app.state.chat.store.get_message(row["message_id"])
        assert msg["role"] == "assistant" and msg["text"].strip()[:20] in answer["text"]
        wait_for(lambda: slack_web.ephemeral)
        assert slack_web.ephemeral[0]["user"] == "U0ANA" and "helpful" in slack_web.ephemeral[0]["text"]
        assert len(slack_web.ephemeral) == 1
        # Not helpful: saved at once, then the dialog; its reasons and comment complete the same rating.
        raw, headers = click(prompt, value="down", trigger="trig-2", action_ts="1700000101.1")
        webhook(client, slack_hub, raw, headers)
        wait_for(lambda: slack_web.views)
        assert live(engine)[0]["rating"] == "negative"
        view = slack_web.views[0]["view"]
        assert slack_web.views[0]["trigger_id"] == "trig-2" and view["callback_id"] == cfb.DIALOG_ID
        raw, headers = submit(view)
        assert webhook(client, slack_hub, raw, headers).text == ""      # the dialog closes
        wait_for(lambda: live(engine)[0]["comment"] == "Cite the price list")
        row = live(engine)[0]
        assert json.loads(row["reason_ids"]) == ["missing_source"] and row["revision"] == 3
        # Ben, linked, rates the same answer: his own record. Nothing is ever posted in the thread.
        raw, headers = click(prompt, user="U0BEN", trigger="trig-3", action_ts="1700000102.1")
        webhook(client, slack_hub, raw, headers)
        wait_for(lambda: len(live(engine)) == 2)
        assert len([p for p in slack_web.posts if p.get("thread_ts")]) == 3
    # After a restart the ratings are still there, and the review page shows where they came from.
    from hubzoid.access import store_for

    store_for(slack_hub).grant("ana@example.org", slack_hub.name, "manage_access", actor="t")
    from tests.chat_helpers import as_people, who

    as_people(monkeypatch)
    with TestClient(build_app()) as client:
        items = client.get(f"/api/hubs/{hub_id}/feedback?source=slack", headers=who("ana")).json()["items"]
    assert {(i["author"], i["rating"]) for i in items} == {("Ana Example", "negative"), ("Ben Example", "positive")}


@pytest.mark.slow
def test_slack_refuses_forged_mismatched_and_unlinked_clicks(slack_hub, slack_web):
    with TestClient(build_app()) as client:
        mention(client, slack_hub)
        prompt = wait_for(lambda: next((p for p in slack_web.posts if p.get("blocks")), None))
        engine = client.app.state.chat.engine.engine
        raw, headers = click(prompt)
        bad = dict(headers, **{"x-slack-signature": "v0=" + "0" * 64})
        assert webhook(client, slack_hub, raw, bad).status_code == 401
        # The answer message itself is not the rating prompt; another run's id is not this prompt's.
        answer = next(p for p in slack_web.posts if not p.get("blocks") and "Accepted" not in p["text"])
        for forged in (dict(prompt, ts=answer["ts"]), prompt):
            raw, headers = click(forged, trigger=f"t-{forged['ts']}", action_ts=forged["ts"],
                                 block=None if forged is not prompt else "hzfb:another-run")
            webhook(client, slack_hub, raw, headers)
        # Someone without a link (or from another workspace) gets a private notice, nothing saved.
        raw, headers = click(prompt, user="U0NOBODY", trigger="t-x", action_ts="1700000200.1")
        webhook(client, slack_hub, raw, headers)
        wait_for(lambda: len(slack_web.ephemeral) >= 3)
        rows = store.list_deliveries(slack_hub, config.hub_of(slack_hub), "slack-main")["items"]
        reasons = {r["reason"] for r in rows if r["kind"] == "feedback"}
        assert {"target_mismatch", "identity_unmapped"} <= reasons
        assert live(engine) == []
        assert any("link your Slack account" in e["text"] for e in slack_web.ephemeral)


def test_feedback_only_on_providers_that_have_it(tmp_path):
    from tests.test_channels_chat import FakeAdapter
    from hubzoid.channels import registry

    root = tmp_path / "h"
    root.mkdir()
    (root / "AGENTS.md").write_text("---\nname: H\n---\nH.\n")
    registry.register(FakeAdapter())
    try:
        fake = config.load_text(root, config.render({"version": 1, "id": "f", "provider": "fake", "agent": "main",
                                                      "feedback": True, "provider_config": {"secret_ref": "env:X"}}))
        assert not fake.valid and any("not available" in e["message"] for e in fake.errors)
    finally:
        registry.unregister("fake")


# -- WhatsApp ---------------------------------------------------------------------------------

def reaction(mid, target, emoji="👍", ts=None):
    return {"from": SENDER, "id": mid, "timestamp": str(int(ts if ts is not None else time.time())),
            "type": "reaction", "reaction": {"message_id": target, "emoji": emoji}}


def quoted(mid, target, body, ts=None):
    return {"from": SENDER, "id": mid, "timestamp": str(int(ts if ts is not None else time.time())), "type": "text",
            "text": {"body": body}, "context": {"from": "15550100", "id": target}}


def with_feedback(hub_dir, on=True):
    conn = config.active(hub_dir, "whatsapp-desk")
    data = config.load_text(hub_dir, config.render({**conn.config.model_dump(), "feedback": on})).data
    config.apply(hub_dir, data, expected_revision=conn.revision, actor="test")
    return config.active(hub_dir, "whatsapp-desk")


def approved_reply(hub_dir, meta, work):     # noqa: F811 - the fixture's fake Meta
    from hubzoid.work_items import store as wstore
    from hubzoid.work_items.errors import Parked

    _mode(hub_dir, "approve")
    with pytest.raises(Parked) as parked:
        _post(hub_dir, work)
    op = wstore.get_op(hub_dir, parked.value.operation_id)
    wstore.update_op(hub_dir, op["id"], approval_json={"digest": op["args_digest"], "approver": "ops@desk.example"})
    _post(hub_dir, work)
    return op


def ops_engine(hub_dir):
    from hubzoid import db

    return db.operational_engine(hub_dir)


def test_whatsapp_reactions_and_replies_rate_the_exact_reply(hub_dir, meta, started):   # noqa: F811
    from hubzoid.channels.whatsapp import adapter as wa

    conn, work = _released(hub_dir, meta)
    conn = with_feedback(hub_dir)
    op = approved_reply(hub_dir, meta, work)
    from hubzoid.work_items import operations as ops

    # The approver approved the exact text, rating line included; that text is what was sent.
    assert ops.row_arguments(hub_dir, op)["text"].endswith(cfb.WHATSAPP_FOOTER)
    assert meta.sent[0]["text"]["body"].endswith(cfb.WHATSAPP_FOOTER) and len(meta.sent) == 1
    origin = wa.Origin.from_json(store.get_thread(hub_dir, op["target_json"]["thread"])["origin_json"])
    window = wa.last_inbound_at(conn, origin)
    intake_before = len(wa_t.items(hub_dir))
    now = time.time()
    first = payload(reaction("wamid.r1", "wamid.out1", "👍🏽", ts=now))
    post(conn, first)
    post(conn, first)                                   # Meta redelivers the same bytes
    route_all(hub_dir)
    [row] = live(ops_engine(hub_dir))
    assert (row["source"], row["target_kind"], row["author_kind"], row["rating"]) == ("whatsapp", "channel_reply",
                                                                                      "external", "positive")
    assert row["message_id"] == op["id"] and row["author_id"].startswith("ext:")
    assert row["author_label"] == "WhatsApp sender ··0001" and SENDER not in json.dumps(row)
    # A swipe-reply that starts with 👎 carries the comment; an older event arriving late changes nothing.
    post(conn, payload(quoted("wamid.r2", "wamid.out1", "👎 Friday is too late for us", ts=now + 5)))
    post(conn, payload(reaction("wamid.r0", "wamid.out1", "👍", ts=now - 60)))
    route_all(hub_dir)
    [row] = live(ops_engine(hub_dir))
    assert (row["rating"], row["comment"]) == ("negative", "Friday is too late for us")
    # Removing the reaction removes the rating. Nothing was ever sent back, and no Intake item was made.
    post(conn, payload(reaction("wamid.r3", "wamid.out1", "", ts=now + 10)))
    route_all(hub_dir)
    assert live(ops_engine(hub_dir)) == [] and len(meta.sent) == 1
    assert len(wa_t.items(hub_dir)) == intake_before
    assert wa.last_inbound_at(conn, origin) == window      # a rating never extends the 24-hour window
    # A reaction to anything else is an ordinary message, kept for review as before.
    post(conn, payload(reaction("wamid.r4", "wamid.someone-else", ts=now + 20)))
    route_all(hub_dir)
    assert len(wa_t.items(hub_dir)) == intake_before + 1
    assert live(ops_engine(hub_dir)) == []


def test_whatsapp_review_never_shows_a_customer_as_an_account(hub_dir, meta, started):   # noqa: F811
    conn, work = _released(hub_dir, meta)
    conn = with_feedback(hub_dir)
    approved_reply(hub_dir, meta, work)
    post(conn, payload(quoted("wamid.r1", "wamid.out1", "👎 Wrong date")))
    route_all(hub_dir)
    svc = cfb._service(conn)
    reader = type("R", (), {"id": "u_ops", "email": "ops@desk.example", "groups": frozenset()})()
    hidden = svc.list_feedback(reader=reader, intake_visible=False)["items"][0]
    assert hidden["comment"] is None and hidden["excerpt"] is None and hidden["context"] is None
    assert hidden["author"] == "WhatsApp sender ··0001" and hidden["author_kind"] == "external"
    shown = svc.list_feedback(reader=reader, intake_visible=True)["items"][0]
    assert shown["comment"] == "Wrong date" and "We will confirm by Friday" in shown["excerpt"]
    assert shown["context"]["kind"] == "intake" and shown["context"]["intake_id"]
    assert shown["author_id"] not in ("ops@desk.example", "u_ops") and "@" not in shown["author_id"]


def test_whatsapp_reply_waiting_for_approval_keeps_its_text(hub_dir, meta, started):   # noqa: F811
    from hubzoid.work_items import operations as ops
    from hubzoid.work_items import store as wstore
    from hubzoid.work_items.errors import Parked

    conn, work = _released(hub_dir, meta)
    _mode(hub_dir, "approve")
    with pytest.raises(Parked) as parked:
        _post(hub_dir, work)                       # recorded while ratings are off
    with_feedback(hub_dir)                         # turned on while it waits
    op = wstore.get_op(hub_dir, parked.value.operation_id)
    wstore.update_op(hub_dir, op["id"], approval_json={"digest": op["args_digest"], "approver": "ops@desk.example"})
    _post(hub_dir, work)                           # the workflow resumes: no conflict, the reviewed text
    assert len(meta.sent) == 1 and cfb.WHATSAPP_FOOTER not in meta.sent[0]["text"]["body"]
    assert ops.row_arguments(hub_dir, op)["text"] == meta.sent[0]["text"]["body"]


def test_whatsapp_feedback_off_keeps_todays_behaviour(hub_dir, meta, started):   # noqa: F811
    conn, work = _released(hub_dir, meta)
    approved_reply(hub_dir, meta, work)
    assert cfb.WHATSAPP_FOOTER not in meta.sent[0]["text"]["body"]
    before = len(wa_t.items(hub_dir))
    post(conn, payload(reaction("wamid.r1", "wamid.out1")))
    route_all(hub_dir)
    assert live(ops_engine(hub_dir)) == [] and len(wa_t.items(hub_dir)) == before + 1
    raw = json.dumps(payload(reaction("wamid.r2", "wamid.out1"))).encode()
    bad = "sha256=" + hmac.new(b"wrong", raw, hashlib.sha256).hexdigest()
    from hubzoid.channels import admission
    from hubzoid.channels.errors import ChannelError

    with pytest.raises(ChannelError):
        admission.receive(hub_dir, conn.id, raw, {"X-Hub-Signature-256": bad}, {})
    assert APP_SECRET


def test_whatsapp_disabled_deployment_collects_nothing(hub_dir, meta, started):   # noqa: F811
    from hubzoid.chat import config as chat_config

    conn, work = _released(hub_dir, meta)
    conn = with_feedback(hub_dir)
    approved_reply(hub_dir, meta, work)
    svc = chat_config.service_for(hub_dir)
    doc = svc.active().document
    doc["feedback"]["enabled"] = False
    write_chat_config(hub_dir, doc)
    post(conn, payload(reaction("wamid.r1", "wamid.out1")))
    route_all(hub_dir)
    assert live(ops_engine(hub_dir)) == []
    assert text_msg  # the shared fixtures stay imported for pytest


def wa_count(hub_dir):
    return len(wa_t.items(hub_dir))


def deliver(conn, hub_dir, *messages):
    """Signed Meta webhooks through real admission, then routing."""
    for m in messages:
        post(conn, payload(m))
    route_all(hub_dir)


def test_whatsapp_late_older_events_never_win(hub_dir, meta, started):   # noqa: F811
    conn, work = _released(hub_dir, meta)
    conn = with_feedback(hub_dir)
    approved_reply(hub_dir, meta, work)
    intake = wa_count(hub_dir)
    rows = lambda: [(r["rating"]) for r in live(ops_engine(hub_dir))]   # noqa: E731
    up10 = reaction("wamid.o1", "wamid.out1", "👍", ts=10)
    deliver(conn, hub_dir, up10, up10)                                  # also delivered twice: one rating
    deliver(conn, hub_dir, reaction("wamid.o2", "wamid.out1", "👍", ts=30))   # newer, same choice
    deliver(conn, hub_dir, reaction("wamid.o3", "wamid.out1", "👎", ts=20))   # older, arrives late
    assert rows() == ["positive"]
    deliver(conn, hub_dir, reaction("wamid.o4", "wamid.out1", "", ts=40))     # removed
    deliver(conn, hub_dir, reaction("wamid.o5", "wamid.out1", "", ts=50))     # removed again, later
    deliver(conn, hub_dir, reaction("wamid.o6", "wamid.out1", "👎", ts=45))   # older than that removal
    assert rows() == []
    deliver(conn, hub_dir, reaction("wamid.o7", "wamid.out1", "👍", ts=60))   # genuinely newer
    assert rows() == ["positive"]
    assert wa_count(hub_dir) == intake and len(meta.sent) == 1


def test_whatsapp_removal_before_the_rating_arrives(hub_dir, meta, started):   # noqa: F811
    conn, work = _released(hub_dir, meta)
    conn = with_feedback(hub_dir)
    approved_reply(hub_dir, meta, work)
    intake = wa_count(hub_dir)
    deliver(conn, hub_dir, reaction("wamid.b1", "wamid.out1", "", ts=30))   # the removal comes first
    deliver(conn, hub_dir, reaction("wamid.b2", "wamid.out1", "👍", ts=10))  # then the original vote
    assert live(ops_engine(hub_dir)) == [] and wa_count(hub_dir) == intake


def test_whatsapp_collection_off_removal_works_and_ratings_are_not_intake(hub_dir, meta, started,  # noqa: F811
                                                                          monkeypatch):
    from hubzoid.chat import config as chat_config
    from hubzoid.chat.feedback import FeedbackService

    conn, work = _released(hub_dir, meta)
    conn = with_feedback(hub_dir)
    approved_reply(hub_dir, meta, work)
    deliver(conn, hub_dir, reaction("wamid.c1", "wamid.out1", "👍", ts=10))
    assert len(live(ops_engine(hub_dir))) == 1
    svc = chat_config.service_for(hub_dir)
    active = svc.active()
    doc = active.document
    doc["feedback"]["enabled"] = False
    write_chat_config(hub_dir, doc)
    intake = wa_count(hub_dir)
    deliver(conn, hub_dir, reaction("wamid.c2", "wamid.out1", "", ts=20))     # the person takes it back
    deliver(conn, hub_dir, reaction("wamid.c3", "wamid.out1", "", ts=25))     # and again
    assert live(ops_engine(hub_dir)) == [] and wa_count(hub_dir) == intake
    deliver(conn, hub_dir, reaction("wamid.c4", "wamid.out1", "👎", ts=30),   # a new rating while off
            quoted("wamid.c5", "wamid.out1", "👎 still wrong", ts=31))
    assert live(ops_engine(hub_dir)) == [] and wa_count(hub_dir) == intake
    # Turned off between the check and the save: still consumed, never Intake work.
    monkeypatch.setattr(FeedbackService, "enabled", lambda self: True)
    deliver(conn, hub_dir, reaction("wamid.c6", "wamid.out1", "👍", ts=40))
    assert live(ops_engine(hub_dir)) == [] and wa_count(hub_dir) == intake
    # A reaction to anything else is still an ordinary message.
    deliver(conn, hub_dir, reaction("wamid.c7", "wamid.not-ours", "👍", ts=50))
    assert wa_count(hub_dir) == intake + 1 and len(meta.sent) == 1


def test_a_hyphenated_channel_can_be_granted_in_settings(slack_hub):
    """Found in the HUB-140 E2E: "slack-main" never reached the capability list
    (the permission id check refused the hyphen), so Settings answered
    "Unknown permission for this hub" when an administrator granted it."""
    from hubzoid import capabilities

    rows = {r["permission"]: r for r in capabilities.catalog(slack_hub)}
    assert rows["channel_slack-main"]["label"] == "Start work from slack-main (slack)"
