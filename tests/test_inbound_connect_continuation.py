"""The WhatsApp connection journey end to end, with fakes only.

WhatsApp sender -> roster -> mapped account -> the agent's connect_account
call (the fake bridge runs the real tool under the identity the bridge would
derive) -> the link reaches WhatsApp -> a wrong browser account is refused ->
Open WebUI verification -> success page -> the outbox sends one WhatsApp
confirmation with a one-use YES continuation -> YES re-runs the waiting request
once, as a fresh turn.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient as FastClient
from starlette.testclient import TestClient

from hubzoid import _request_ctx, connect_journey, connections
from hubzoid.access import Identity, identity_scope
from hubzoid.connect_journey import notify, store
from hubzoid.inbound.harness import WhatsAppConfig, build_app
from tests import connect_helpers as h

ALICE, BOB = "alice@example.org", "bob@example.org"
PHONE, PHONE_B = "919800000001", "919800000002"
SECRET = "wa-secret"
ASK = "Connect my Gmail and summarise today's inbox"


class _Hub(type(Path())):
    """A hub path that can also carry the test's fakes."""


@pytest.fixture
def hub(tmp_path, monkeypatch):
    hub = _Hub(tmp_path / "nurturehub")
    hub.mkdir()
    db = tmp_path / "webui.db"
    h.seed_owui(db, users=[("ua", ALICE), ("ub", BOB)], secret=SECRET, servers=[
        {"id": "gmail", "name": "Gmail", "url": "https://gmail-mcp.example.org/mcp"}])
    h.owui_env(monkeypatch, db, SECRET)
    h.isolated_store(tmp_path, monkeypatch)
    monkeypatch.setenv("HUBZOID_CONNECT_JOURNEY", "true")
    monkeypatch.setenv("WEBUI_URL", "https://hub.example.org")
    monkeypatch.setenv("HUBZOID_RESTRICTED_SURFACES", "owui,web,api,mcp,whatsapp")
    connections.set_gate(connections.Connections(client=None, allowed=()))
    hub.db = db
    hub.roster = {PHONE: {"email": ALICE, "groups": ["connector_gmail"]},
                  PHONE_B: {"email": BOB, "groups": ["connector_gmail"]}}
    return hub


class FakeBridge:
    """Stands in for the hub bridge: derives the identity from the forwarded
    headers exactly as server.py does, and when the message asks to connect,
    runs the real connect_account tool."""

    def __init__(self, hub, *, echo_link=False):
        self.hub = hub
        self.echo_link = echo_link
        self.calls = []

    def __call__(self, **kw):
        self.calls.append(kw)
        text = kw["messages"][-1]["content"]
        ident = Identity.make(user=kw["user_email"], groups=kw["groups"], surface=kw["surface"])
        if "connect" not in text.lower():
            return f"(answered: {text})"
        with identity_scope(ident), _request_ctx.chat_scope(kw["chat_id"]):
            try:
                r = connect_journey.start(self.hub, app="gmail")
            except connect_journey.JourneyError as err:
                return err.message
        if r["state"] == "connected":
            return f"(answered: {text})"
        return f"Here you go: {r['url']}" if self.echo_link else "Open the link I sent to connect."


def _app(hub, bridge, sent, *, journeys=None):
    wa = WhatsAppConfig(verify_token="VT", app_secret="SEC", token="TKN", phone_number_id="PNID",
                        send_text=lambda **kw: sent.append(kw) or {}, mark_read=lambda **kw: {})
    return build_app(hub_dir=hub, bridge_url="http://bridge/v1", api_key="k", model="m",
                     resolver=lambda surface, handle: hub.roster.get(handle), whatsapp=wa,
                     dispatch_fn=bridge, connect_journeys=journeys)


def _post(app, phone, mid, body):
    payload = {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
        "messaging_product": "whatsapp", "contacts": [{"wa_id": phone}],
        "messages": [{"from": phone, "id": mid, "type": "text", "text": {"body": body}}]}}]}]}
    raw = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(b"SEC", raw, hashlib.sha256).hexdigest()
    r = TestClient(app).post("/webhooks/whatsapp", content=raw,
                             headers={"X-Hub-Signature-256": sig, "Content-Type": "application/json"})
    assert r.status_code == 200


def _pages(hub):
    app = FastAPI()
    app.include_router(connect_journey.build_router(
        hub, session_email=lambda request: request.headers.get("x-test-session", "")))
    return FastClient(app, base_url="https://hub.example.org", follow_redirects=False)


def _tick(hub, sent_by_outbox):
    return notify.tick(hub, hub=hub.name, resolver=lambda s, handle: hub.roster.get(handle),
                       send=lambda **kw: sent_by_outbox.append(kw))


def test_whatsapp_gmail_journey_end_to_end(hub):
    sent, outbox = [], []
    bridge = FakeBridge(hub)  # the model forgets to repeat the link
    app = _app(hub, bridge, sent)

    # 1. "Connect my Gmail ..." from a roster-mapped number.
    _post(app, PHONE, "wamid.1", ASK)
    assert bridge.calls[-1]["user_email"] == ALICE and bridge.calls[-1]["surface"] == "whatsapp"
    (j,) = store.open_for(hub, subject=ALICE, app="gmail")
    link = connect_journey.link_url(j["id"])
    assert sent[-1]["to"] == PHONE and link in sent[-1]["text"]  # the harness added it
    assert "oauth" not in sent[-1]["text"]
    assert j["continuation"] == ASK and j["handle"] == PHONE

    # 2. The link opened while signed in as someone else is refused.
    pages = _pages(hub)
    path = f"/portal/connect/{j['id']}"
    assert pages.get(path, headers={"x-test-session": BOB}).status_code == 403

    # 3. The right account starts, consents, returns.
    assert pages.get(path, headers={"x-test-session": ALICE}).status_code == 200
    r = pages.post(path + "/start", headers={"x-test-session": ALICE,
                                             "origin": "https://hub.example.org"})
    assert r.status_code == 303
    assert _tick(hub, outbox) == 0  # nothing to say while consent is in progress
    h.connect(hub.db, user_id="ua", server_id="gmail", secret=SECRET, access_token="AT-alice")
    done = pages.get(path + "/done?status=success", headers={"x-test-session": ALICE})
    assert done.status_code == 200 and "Gmail is connected" in done.text

    # 4. WhatsApp confirmation, once, with the one-use continuation offer.
    assert _tick(hub, outbox) == 1
    assert outbox[-1]["to"] == PHONE
    assert outbox[-1]["text"].startswith("Gmail is connected.")
    assert "Reply YES within 10 minutes to continue: " + ASK in outbox[-1]["text"]
    assert _tick(hub, outbox) == 0 and len(outbox) == 1

    # 5. YES re-runs the waiting request once, as a fresh turn.
    _post(app, PHONE, "wamid.2", "Yes!")
    assert bridge.calls[-1]["messages"][-1]["content"] == ASK
    assert bridge.calls[-1]["user_email"] == ALICE
    assert "(answered: " + ASK + ")" in sent[-1]["text"]  # connected now: no new link
    # 6. A second YES is just a message.
    _post(app, PHONE, "wamid.3", "yes")
    assert bridge.calls[-1]["messages"][-1]["content"] == "yes"


def test_the_link_is_not_repeated_when_the_model_already_sent_it(hub):
    sent = []
    app = _app(hub, FakeBridge(hub, echo_link=True), sent)
    _post(app, PHONE, "wamid.1", ASK)
    (j,) = store.open_for(hub, subject=ALICE, app="gmail")
    assert sent[-1]["text"].count(j["id"]) == 1


def _connected_with_offer(hub, app, sent, outbox):
    _post(app, PHONE, "wamid.a", ASK)
    (j,) = store.open_for(hub, subject=ALICE, app="gmail")
    store.mark_started(hub, j["id"], ttl=600)
    h.connect(hub.db, user_id="ua", server_id="gmail", secret=SECRET, access_token="AT")
    assert _tick(hub, outbox) == 1  # the poller finishes it without the done page
    return j


def test_any_other_reply_clears_the_offer(hub):
    sent, outbox = [], []
    bridge = FakeBridge(hub)
    app = _app(hub, bridge, sent)
    j = _connected_with_offer(hub, app, sent, outbox)
    _post(app, PHONE, "wamid.b", "thanks")
    assert store.get(hub, j["id"])["continuation_status"] == "declined"
    _post(app, PHONE, "wamid.c", "YES")
    assert bridge.calls[-1]["messages"][-1]["content"] == "YES"


def test_another_sender_cannot_take_the_continuation(hub):
    sent, outbox = [], []
    bridge = FakeBridge(hub)
    app = _app(hub, bridge, sent)
    j = _connected_with_offer(hub, app, sent, outbox)
    _post(app, PHONE_B, "wamid.b", "YES")
    assert bridge.calls[-1]["messages"][-1]["content"] == "YES"
    assert bridge.calls[-1]["user_email"] == BOB
    assert store.get(hub, j["id"])["continuation_status"] == "offered"


def test_continuation_is_reauthorised_at_use(hub):
    """The roster now maps the number to someone else: the offer is not theirs."""
    sent, outbox = [], []
    bridge = FakeBridge(hub)
    app = _app(hub, bridge, sent)
    _connected_with_offer(hub, app, sent, outbox)
    hub.roster[PHONE] = {"email": BOB, "groups": []}
    _post(app, PHONE, "wamid.b", "YES")
    assert bridge.calls[-1]["messages"][-1]["content"] == "YES"
    assert bridge.calls[-1]["user_email"] == BOB


def test_no_confirmation_when_the_number_no_longer_maps_to_the_account(hub):
    sent, outbox = [], []
    app = _app(hub, FakeBridge(hub), sent)
    _post(app, PHONE, "wamid.a", ASK)
    (j,) = store.open_for(hub, subject=ALICE, app="gmail")
    store.mark_started(hub, j["id"], ttl=600)
    h.connect(hub.db, user_id="ua", server_id="gmail", secret=SECRET, access_token="AT")
    hub.roster[PHONE] = {"email": BOB, "groups": []}
    assert _tick(hub, outbox) == 0 and outbox == []
    assert store.get(hub, j["id"])["status"] == "connected"


def test_started_but_unfinished_gets_one_not_connected_note(hub):
    sent, outbox = [], []
    app = _app(hub, FakeBridge(hub), sent)
    _post(app, PHONE, "wamid.a", ASK)
    (j,) = store.open_for(hub, subject=ALICE, app="gmail")
    store.mark_started(hub, j["id"], ttl=600)
    later = time.time() + 700
    assert notify.tick(hub, hub=hub.name, resolver=lambda s, x: hub.roster.get(x),
                       send=lambda **kw: outbox.append(kw), now=later) == 1
    assert outbox[-1]["text"].startswith("Gmail was not connected.")
    assert "YES" not in outbox[-1]["text"]
    assert notify.tick(hub, hub=hub.name, resolver=lambda s, x: hub.roster.get(x),
                       send=lambda **kw: outbox.append(kw), now=later + 5) == 0
    assert store.get(hub, j["id"])["continuation"] is None


def test_unopened_expired_and_cancelled_links_end_silently(hub):
    sent, outbox = [], []
    app = _app(hub, FakeBridge(hub), sent)
    _post(app, PHONE, "wamid.a", ASK)
    (j,) = store.open_for(hub, subject=ALICE, app="gmail")
    assert notify.tick(hub, hub=hub.name, resolver=lambda s, x: hub.roster.get(x),
                       send=lambda **kw: outbox.append(kw), now=time.time() + 700) == 0
    assert store.get(hub, j["id"])["status"] == "expired"
    _post(app, PHONE, "wamid.b", ASK)
    (j2,) = store.open_for(hub, subject=ALICE, app="gmail")
    store.transition(hub, j2["id"], frm=store.OPEN, to="cancelled")
    assert _tick(hub, outbox) == 0 and outbox == []


def test_a_send_failure_is_not_retried(hub):
    sent = []
    app = _app(hub, FakeBridge(hub), sent)
    _post(app, PHONE, "wamid.a", ASK)
    (j,) = store.open_for(hub, subject=ALICE, app="gmail")
    store.mark_started(hub, j["id"], ttl=600)
    h.connect(hub.db, user_id="ua", server_id="gmail", secret=SECRET, access_token="AT")

    def boom(**kw):
        raise RuntimeError("meta down")

    assert notify.tick(hub, hub=hub.name, resolver=lambda s, x: hub.roster.get(x), send=boom) == 0
    assert store.get(hub, j["id"])["notified"] is not None


def test_poller_runs_with_the_app_lifespan_only_when_on(hub, monkeypatch):
    sent = []
    app = _app(hub, FakeBridge(hub), sent, journeys=True)
    poller = app.state.connect_poller
    with TestClient(app):
        assert poller._thread is not None and poller._thread.is_alive()
    assert poller._thread is None
    monkeypatch.delenv("HUBZOID_CONNECT_JOURNEY")
    off = _app(hub, FakeBridge(hub), sent)
    assert not hasattr(off.state, "connect_poller")


def test_journeys_off_leaves_whatsapp_exactly_as_before(hub, monkeypatch):
    monkeypatch.delenv("HUBZOID_CONNECT_JOURNEY")
    sent = []
    bridge = FakeBridge(hub)
    app = _app(hub, bridge, sent)
    _post(app, PHONE, "wamid.1", "YES")
    assert bridge.calls[-1]["messages"][-1]["content"] == "YES"
    assert sent[-1]["text"] == "(answered: YES)"
    # With the switch off no journey can be created by the tool either.
    from hubzoid.tools import connect_tools
    import types
    assert connect_tools.make(types.SimpleNamespace(hub_dir=hub)) == []
