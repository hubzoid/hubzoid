"""``request.created`` webhooks: from the persisted HITL events, to the people
who may act, once, without the request's content; retries keep running."""
from __future__ import annotations

import time

from hubzoid.chat import feedback as fb
from tests.test_chat_feedback import client, hub  # noqa: F401 - fixtures
from tests.test_chat_operations import new_conv, snap


def _target(service, scope, owner, events):
    body = {"type": "webhook", "recipient_ref": "https://hooks.example.com/hz",
            "connection_ref": "env:HZ_HOOK", "events": events}
    service.put_target(scope, "ops", body, owner_id=owner, expected_revision="absent")
    service.verify_target(scope, "ops", send=lambda t, p: (True, "http_200"))
    return service.put_target(scope, "ops", {**body, "enabled": True}, owner_id=owner, expected_revision=2)


def test_request_created_reaches_the_people_who_may_act(client, hub, monkeypatch):
    from hubzoid.hitl import gate
    from hubzoid.hitl import service as hitl

    monkeypatch.setenv("HZ_HOOK", "s3cret")
    monkeypatch.setattr(fb.socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("93.184.216.34", 443))])
    service = client.app.state.chat.feedback
    owner = snap(client, new_conv(client)[0])["conversation"]["owner_id"]
    scope = service.scope_of("own", owner, manager=False)
    _target(service, scope, owner, ["request.created"])
    hub_id = gate.hub_id_of(hub)
    mine = hitl.create(hub, hub_id, hitl.Caller(account_id=owner, email="x@example.com", origin="api"),
                       kind="approval", title="Pay invoice 4411 to ACME", tool="pay", arguments={"amount": 900})
    hitl.create(hub, hub_id, hitl.Caller(account_id="someone-else", email="y@example.com", origin="api"),
                kind="question", title="Their question", question="?")
    seen = []

    def capture(target, payload):
        seen.append(payload)
        return True, "http_200"

    assert service.ingest_requests() == 1
    assert service.ingest_requests() == 0  # idempotent
    service._request_cursor = 0
    assert service.ingest_requests() == 0  # also after a restart re-reads the log
    service.deliver_due(send=capture)
    assert len(seen) == 1
    p = seen[0]
    assert p["type"] == "request.created" and p["request_id"] == mine["id"] and p["kind"] == "approval"
    # Never the request's content. Ids and the event time are random, so they
    # are left out of the text check (a request id once held "900", and so did
    # an occurred_at of ...712.8900657, each failing this by chance).
    assert "title" not in p and "arguments" not in p
    assert isinstance(p["occurred_at"], float)
    content = str({k: v for k, v in p.items()
                   if k not in ("id", "occurred_at") and not k.endswith("_id")})
    assert "ACME" not in content and "900" not in content


def test_targets_that_do_not_want_requests_get_nothing_and_retries_wait(client, hub, monkeypatch):
    from hubzoid.hitl import gate
    from hubzoid.hitl import service as hitl

    monkeypatch.setenv("HZ_HOOK", "s3cret")
    monkeypatch.setattr(fb.socket, "getaddrinfo", lambda *a, **k: [(0, 0, 0, "", ("93.184.216.34", 443))])
    service = client.app.state.chat.feedback
    owner = snap(client, new_conv(client)[0])["conversation"]["owner_id"]
    scope = service.scope_of("own", owner, manager=False)
    _target(service, scope, owner, ["run.completed"])
    assert not service.delivery_needed()
    hitl.create(hub, gate.hub_id_of(hub), hitl.Caller(account_id=owner, email="x@example.com", origin="api"),
                kind="question", title="Which region?", question="Which region?")
    assert service.ingest_requests() == 0
    monkeypatch.setattr(client.app.state.chat.engine, "on_terminal", None)
    service.on_run_terminal(run_id="r1", conversation_id="c", branch_id="b", message_id="m", actor_id=owner,
                            status="complete", stopped_by_self=False)
    assert service.delivery_needed() and service.next_due() <= time.time()
    service.deliver_due(send=lambda t, p: (False, "http_503"))
    due = service.next_due()
    assert due is not None and due > time.time()  # a retry is scheduled, and the deliverer waits for it


def test_the_deliverer_keeps_running_for_retries():
    calls = []

    class Svc:
        def ingest_requests(self):
            calls.append("ingest")
            return 0

        def deliver_due(self):
            calls.append("deliver")
            return 0

        def next_due(self):
            return time.time() + 0.1

    from hubzoid.chat.feedback_routes import _Deliverer

    d = _Deliverer(Svc())
    d.start()
    time.sleep(0.6)
    assert calls.count("deliver") >= 3  # it did not stop after three idle passes
    d.stop()
    time.sleep(0.3)
    seen = len(calls)
    time.sleep(0.4)
    assert len(calls) == seen and not d._running  # stopped with the app
    d.kick()
    assert not d._running
