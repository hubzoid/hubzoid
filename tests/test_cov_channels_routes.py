"""Channel management HTTP validation, persistence, errors and provider acknowledgements."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from starlette.testclient import TestClient

from hubzoid.channels import admission, config, replies, router, routes, store
from hubzoid.channels.errors import ChannelError
from hubzoid.channels.models import Challenge
from tests.test_channels_contract import _user, applied, ev, fake as fake, hub_dir as hub_dir, send, yaml_for


@pytest.fixture
def client(hub_dir, monkeypatch):
    async def person(request, root, capability, hub, *, mutation=False):
        if request.headers.get("x-deny"):
            raise HTTPException(403, detail={"code": "permission_denied", "message": capability})
        return SimpleNamespace(email="owner@example.org", org_admin=True)
    async def reload():
        return None
    monkeypatch.setattr(routes.authz, "require_request", person)
    monkeypatch.setattr(router, "reload_transports", reload)
    monkeypatch.setattr(router, "kick", lambda *a: None)
    from hubzoid import editing
    monkeypatch.setattr(editing, "check", lambda *a: None)
    app = FastAPI()
    routes.mount(app, hub_dir)
    with TestClient(app) as c:
        yield c


BASE = "/api/hubs/desk/channels"


def test_configuration_create_read_update_pause_and_validate(client, hub_dir):
    assert client.get(BASE, headers={"x-deny": "1"}).status_code == 403
    assert client.get(BASE.replace("desk", "other")).json()["detail"]["code"] == "hub_not_found"
    r = client.post(BASE + "/validate", json={"text": yaml_for()})
    assert r.status_code == 200 and r.json()["valid"]
    r = client.post(BASE + "/validate", json={"config": {"provider": "missing"}})
    assert r.status_code == 200 and not r.json()["valid"]
    r = client.put(BASE + "/fake-main/config", json={"text": yaml_for(), "expected_revision": "absent"})
    assert r.status_code == 201 and r.json()["created"]
    rev = r.json()["revision"]
    r = client.get(BASE + "/fake-main/config")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.json()["config"]["provider"] == "fake"
    r = client.put(BASE + "/fake-main/config", json={"config": config.load_text(hub_dir, yaml_for()).data,
                                                     "expected_revision": str(rev)})
    assert r.status_code == 200 and not r.json()["created"]
    rev = r.json()["revision"]
    r = client.post(BASE + "/fake-main/enabled", json={"enabled": False, "expected_revision": rev})
    assert r.status_code == 200 and r.json()["status"] == "disabled"
    assert not config.active(hub_dir, "fake-main").config.enabled
    r = client.get(BASE)
    assert r.json()["items"][0]["status"] == "disabled"
    assert r.json()["items"][0]["target"] == "agent:main"


@pytest.mark.parametrize("suffix,method,body,code", [
    ("/fake-main/config", "put", {}, "validation_failed"),
    ("/fake-main/config", "put", {"expected_revision": "bad"}, "validation_failed"),
    ("/fake-main/config", "put", {"expected_revision": [], "text": ""}, "validation_failed"),
    ("/fake-main/config", "put", {"expected_revision": "absent", "text": "id: wrong"}, "validation_failed"),
    ("/fake-main/enabled", "post", {"enabled": "yes", "expected_revision": 1}, "validation_failed"),
    ("/fake-main/enabled", "post", {"enabled": True, "expected_revision": "absent"}, "validation_failed"),
    ("/fake-main/outbox/op/resolve", "post", {"decision": "guess"}, "validation_failed"),
    ("/fake-main/threads/t/subscription", "put", {"subscribed": "yes"}, "validation_failed"),
])
def test_management_rejects_invalid_inputs(client, suffix, method, body, code):
    r = getattr(client, method)(BASE + suffix, json=body)
    assert r.status_code == 422 and r.json()["detail"]["code"] == code


@pytest.mark.parametrize("body,message", [("[1]", "Send a JSON object."), ("{", "Send the request body as JSON.")])
def test_json_body_is_an_object(client, body, message):
    r = client.post(BASE + "/validate", content=body)
    assert r.status_code == 400
    assert r.json()["detail"] == {"code": "invalid_request", "message": message}


def test_thread_delivery_subscription_links_and_doctor(client, hub_dir):
    conn = applied(hub_dir)
    _user(hub_dir, "ana@example.org")
    delivery = send(conn, ev(), delivery_id="http-one").events[0]
    row = store.get_delivery(hub_dir, delivery["id"])
    thread = store.get_thread(hub_dir, row["thread_id"])
    tid = thread["id"]
    r = client.get(BASE + "/fake-main/deliveries?limit=1&status=accepted")
    assert r.status_code == 200 and r.json()["items"][0]["id"] == row["id"]
    assert "event_enc" not in r.json()["items"][0]
    r = client.get(BASE + "/fake-main/threads?limit=1")
    assert r.status_code == 200 and r.json()["items"][0]["id"] == tid
    r = client.get(BASE + f"/fake-main/threads/{tid}")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.json()["origin"]["coordinates"] == {"thread": "t1"}
    r = client.put(BASE + f"/fake-main/threads/{tid}/subscription", json={"subscribed": True,
                                                                                       "expected_revision": thread["revision"]})
    assert r.status_code == 200 and r.json()["subscribed"]
    assert store.get_thread(hub_dir, tid)["subscribed"] == 1
    r = client.post(BASE + "/fake-main/links", json={"external_id": "U1", "account": "ana@example.org"})
    assert r.status_code == 201 and r.json()["account"] == "ana@example.org"
    lid = r.json()["id"]
    assert client.get(BASE + "/fake-main/links").json()["items"][0]["id"] == lid
    assert client.delete(BASE + f"/fake-main/links/{lid}").status_code == 204
    assert client.get(BASE + "/fake-main/links").json() == {"items": []}
    r = client.post(BASE + "/fake-main/doctor", json={})
    assert r.status_code == 200 and any(c["id"] == "fake" and c["status"] == "ok" for c in r.json()["checks"])
    assert client.get(BASE + "/fake-main/outbox").json() == {"items": []}


@pytest.mark.parametrize("result,expected_status,expected_body", [
    (Challenge(body="hello", media_type="text/plain", status=202), 202, "hello"),
    (Challenge(body={"challenge": "ok"}), 200, {"challenge": "ok"}),
])
def test_provider_challenge_response(client, hub_dir, monkeypatch, result, expected_status, expected_body):
    applied(hub_dir)
    monkeypatch.setattr(admission, "receive", lambda *a: result)
    r = client.post(BASE + "/fake-main/webhook", content=b"raw")
    assert r.status_code == expected_status
    assert (r.json() if isinstance(expected_body, dict) else r.text) == expected_body


def test_webhook_records_before_ack_and_refuses_large_body(client, hub_dir, fake, monkeypatch):
    from tests.channel_fake import delivery, headers
    applied(hub_dir)
    raw, did = delivery(ev(), delivery_id="ack")
    r = client.post(BASE + "/fake-main/webhook", content=raw, headers=headers("fake-signing-value", raw, did))
    assert r.status_code == 200
    assert store.list_deliveries(hub_dir, "desk", "fake-main")["items"][0]["delivery_id"] == did
    monkeypatch.setattr(fake, "body_limit", 2)
    r = client.post(BASE + "/fake-main/webhook", content=b"abc")
    assert r.status_code == 413 and r.json()["detail"]["code"] == "delivery_too_large"
    monkeypatch.setattr(config, "active", lambda *a: (_ for _ in ()).throw(ChannelError("unavailable", "Retry", status=503)))
    r = client.post(BASE + "/fake-main/webhook", content=b"a")
    assert r.status_code == 503 and r.json()["detail"]["message"] == "Retry"


@pytest.mark.asyncio
async def test_call_maps_work_item_errors_but_propagates_programming_errors():
    from hubzoid.work_items.errors import WorkItemError
    def fail():
        raise WorkItemError("denied", "No access", status=403)
    with pytest.raises(HTTPException) as exc:
        await routes.call(fail)
    assert exc.value.status_code == 403 and exc.value.detail["code"] == "denied"
    with pytest.raises(ZeroDivisionError):
        await routes.call(lambda: 1 / 0)


def test_redrive_refusals_and_lost_update(hub_dir, monkeypatch):
    base = {"hub": "desk", "connection_id": "fake-main", "event_enc": "encrypted"}
    for row, code in [(None, "not_found"), ({**base, "hub": "other"}, "not_found"),
                      *[({**base, "status": s}, "already_routed") for s in ("routed", "routing", "accepted", "waiting")],
                      *[({**base, "status": s}, "not_redrivable") for s in ("duplicate", "quarantined")],
                      ({**base, "status": "denied", "event_enc": None}, "not_redrivable")]:
        monkeypatch.setattr(store, "get_delivery", lambda *a, row=row: row)
        with pytest.raises(ChannelError) as exc:
            routes.redrive(hub_dir, "desk", "fake-main", "d1", "owner")
        assert exc.value.code == code
    monkeypatch.setattr(store, "get_delivery", lambda *a: {**base, "status": "denied"})
    monkeypatch.setattr(store, "update_delivery", lambda *a, **kw: None)
    with pytest.raises(ChannelError) as exc:
        routes.redrive(hub_dir, "desk", "fake-main", "d1", "owner")
    assert exc.value.code == "revision_conflict"
