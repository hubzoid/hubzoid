"""The WhatsApp channel with a fake Meta Cloud API: signed deliveries, every
message into Intake, receipts, the 24-hour window, approve-only replies and
media from Meta's hosts only. No network and no model."""
from __future__ import annotations

import hashlib
import hmac
import json
import textwrap
import time

import httpx
import pytest

from hubzoid.channels import admission, config, store
from hubzoid.channels.email import transports
from hubzoid.channels.errors import ChannelError
from hubzoid.channels.whatsapp import adapter as wa

APP_SECRET = "meta-app-secret-value"     # gitleaks:allow (a test value)
VERIFY = "meta-verify-value"             # gitleaks:allow
TOKEN = "EAAtest-token-value"            # gitleaks:allow
PNID, WABA, SENDER = "100000000000002", "100000000000001", "919800000001"


class FakeMeta:
    def __init__(self):
        self.sent: list[dict] = []
        self.fail: int | None = None
        self.media_url = "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=1"
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(str(request.url))
        assert request.headers.get("authorization") == f"Bearer {TOKEN}"
        path = request.url.path
        if path.endswith(f"/{PNID}/messages"):
            if self.fail:
                code, self.fail = self.fail, None
                return httpx.Response(code, json={"error": {"code": 131047}})
            body = json.loads(request.content)
            self.sent.append(body)
            return httpx.Response(200, json={"messages": [{"id": f"wamid.out{len(self.sent)}"}]})
        if path.endswith("/media-1"):
            assert request.url.params["phone_number_id"] == PNID
            return httpx.Response(200, json={"url": self.media_url, "mime_type": "application/pdf"})
        if request.url.host in wa.MEDIA_HOSTS:
            return httpx.Response(200, content=b"%PDF-1.4 invoice")
        if path.endswith(f"/{PNID}"):
            return httpx.Response(200, json={"display_phone_number": "+1 555 0100", "verified_name": "Desk"})
        return httpx.Response(404, json={"error": {"message": "not faked"}})


@pytest.fixture
def meta(monkeypatch):
    fake = FakeMeta()
    monkeypatch.setattr(transports, "_client", httpx.Client(transport=httpx.MockTransport(fake)))
    return fake


@pytest.fixture
def started(monkeypatch):
    from hubzoid.work_items import operations
    from hubzoid.work_items import service as wi

    monkeypatch.setattr(wi, "start", lambda hub_dir, item: item)
    monkeypatch.setattr(operations, "_approver_still_allowed", lambda hub_dir, hub, who: bool(who))


@pytest.fixture
def hub_dir(tmp_path, monkeypatch):
    for key in ("DATABASE_URL", "HUBZOID_OPERATIONAL_DB", "HUBZOID_PUBLIC_URL", "WEBUI_AUTH",
                "HUBZOID_WHATSAPP_API_URL"):
        monkeypatch.delenv(key, raising=False)
    for k, v in (("WA_APP_SECRET", APP_SECRET), ("WA_VERIFY", VERIFY), ("WA_TOKEN", TOKEN)):
        monkeypatch.setenv(k, v)
    root = tmp_path / "desk"
    (root / "workflows" / "requests").mkdir(parents=True)
    (root / "AGENTS.md").write_text("---\nname: Desk\n---\nThe desk.\n")
    (root / "workflows" / "requests" / "main.py").write_text(textwrap.dedent('''
        from hubzoid import hub, workflow
        from hubzoid.work_items import Route

        @workflow(work_items={"intake": Route(actions=("customer_reply.send",))})
        def requests():
            return {"ok": True}
    '''))
    return root


def wa_conn(hub_dir, **pc):
    provider_config = {"waba_id": WABA, "phone_number_id": PNID, "app_secret_ref": "env:WA_APP_SECRET",
                       "verify_token_ref": "env:WA_VERIFY", "access_token_ref": "env:WA_TOKEN", **pc}
    v = config.load_text(hub_dir, config.render({"version": 1, "id": "whatsapp-desk", "provider": "whatsapp_cloud",
                                                 "enabled": True, "workflow": "requests", "run_as": "workflow",
                                                 "provider_config": provider_config}))
    assert v.valid, v.errors
    config.apply(hub_dir, v.data, expected_revision="absent", actor="test")
    return config.active(hub_dir, "whatsapp-desk")


def payload(*messages, statuses=(), pnid=PNID):
    return {"object": "whatsapp_business_account", "entry": [{"id": WABA, "changes": [{"field": "messages", "value": {
        "messaging_product": "whatsapp", "metadata": {"display_phone_number": "15550100", "phone_number_id": pnid},
        "contacts": [{"wa_id": SENDER, "profile": {"name": "Asha"}}], "messages": list(messages),
        "statuses": list(statuses)}}]}]}


def text_msg(mid="wamid.in1", body="Invoice for order 42 attached", ts=None):
    return {"from": SENDER, "id": mid, "timestamp": str(int(ts if ts is not None else time.time())), "type": "text",
            "text": {"body": body}}


def post(conn, body, secret=APP_SECRET):
    raw = json.dumps(body).encode()
    sig = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return admission.receive(conn.hub_dir, conn.id, raw, {"X-Hub-Signature-256": sig}, {})


def route_all(hub_dir):
    from hubzoid.channels import router

    for row in store.open_deliveries(hub_dir, "desk"):
        router._route_one(hub_dir, row, None)


def items(hub_dir):
    from hubzoid.intake import service

    return service.list_items(hub_dir)["items"]


# -- configuration and verification ------------------------------------------------------

def test_config_rules(hub_dir):
    base = {"version": 1, "id": "w", "provider": "whatsapp_cloud", "workflow": "requests", "run_as": "workflow"}
    pc = {"waba_id": WABA, "phone_number_id": PNID, "app_secret_ref": "env:A", "verify_token_ref": "env:B",
          "access_token_ref": "env:C"}
    v = config.load_text(hub_dir, config.render({**base, "provider_config": {**pc, "graph_version": "v1.0"}}))
    assert not v.valid and "graph_version" in json.dumps(v.errors)
    v = config.load_text(hub_dir, config.render({**base, "provider_config": {
        **pc, "replies": {"outside_window": "free_form"}}}))
    assert not v.valid
    v = config.load_text(hub_dir, config.render({**base, "workflow": None, "agent": "main",
                                                 "run_as": "linked_requester", "provider_config": pc}))
    assert {"/agent", "/run_as"} <= {e["field"] for e in v.errors}


def test_challenge_needs_the_verify_token(hub_dir):
    conn = wa_conn(hub_dir)
    ok = admission.receive(hub_dir, conn.id, b"", {}, {"hub.mode": "subscribe", "hub.verify_token": VERIFY,
                                                        "hub.challenge": "12345"})
    assert ok.body == "12345" and ok.media_type == "text/plain"
    with pytest.raises(ChannelError) as exc:
        admission.receive(hub_dir, conn.id, b"", {}, {"hub.mode": "subscribe", "hub.verify_token": "nope",
                                                       "hub.challenge": "1"})
    assert exc.value.status == 403


def test_signature_covers_the_raw_body(hub_dir):
    conn = wa_conn(hub_dir)
    raw = json.dumps(payload(text_msg())).encode()
    sig = "sha256=" + hmac.new(APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    with pytest.raises(ChannelError) as exc:
        admission.receive(hub_dir, conn.id, raw.replace(b"order 42", b"order 43"), {"x-hub-signature-256": sig}, {})
    assert exc.value.status == 401
    assert admission.receive(hub_dir, conn.id, raw, {"x-hub-signature-256": sig}, {}).status == "accepted"


# -- intake ----------------------------------------------------------------------------------

def test_every_message_waits_for_a_person(hub_dir, meta, started):
    conn = wa_conn(hub_dir)
    post(conn, payload(text_msg()))
    route_all(hub_dir)
    [item] = items(hub_dir)
    assert item["state"] == "quarantined" and item["sender"]["external_id"] == "+" + SENDER
    assert meta.sent == []                       # nothing goes back to the sender


def test_reject_policy_keeps_evidence(hub_dir, meta, started):
    conn = wa_conn(hub_dir, unknown_sender="reject")
    post(conn, payload(text_msg()))
    route_all(hub_dir)
    [item] = items(hub_dir)
    assert item["state"] == "rejected" and item["reason"] == "unknown_sender"


def test_redelivery_and_batches_start_once(hub_dir, meta, started):
    conn = wa_conn(hub_dir)
    body = payload(text_msg("wamid.a"), text_msg("wamid.b", body="Second"))
    post(conn, body)
    again = post(conn, body)
    assert again.status == "duplicate"
    post(conn, payload(text_msg("wamid.a")))     # the same message in another batch
    route_all(hub_dir)
    assert sorted(i["summary"] for i in items(hub_dir)) == ["Invoice for order 42 attached", "Second"]


def test_receipts_never_start_work_and_never_go_back(hub_dir, meta, started):
    conn = wa_conn(hub_dir)
    st = lambda s, t: {"id": "wamid.out1", "status": s, "timestamp": str(t), "recipient_id": SENDER}  # noqa: E731
    post(conn, payload(statuses=[st("read", 30)]))
    post(conn, payload(statuses=[st("sent", 10)]))       # late, older status
    route_all(hub_dir)
    assert items(hub_dir) == []
    state = config.active(hub_dir, conn.id).state
    assert state["delivery_receipts"]["wamid.out1"]["status"] == "read"


def test_another_numbers_message_is_ignored(hub_dir, meta, started):
    conn = wa_conn(hub_dir)
    out = post(conn, payload(text_msg(), pnid="999999999999"))
    assert out.events[0]["reason"] == "phone_number_mismatch"


def test_future_timestamp_is_quarantined_and_never_extends_the_window(hub_dir, meta, started):
    conn = wa_conn(hub_dir)
    post(conn, payload(text_msg(ts=time.time() + 3 * 86400)))
    route_all(hub_dir)
    [item] = items(hub_dir)
    assert item["reason"] == "future_timestamp"
    thread = store.get_thread(hub_dir, item["thread_id"])
    origin = wa.Origin(**{k: thread["origin_json"][k] for k in ("provider", "workspace_id", "thread_key",
                                                                "coordinates")})
    assert wa.last_inbound_at(conn, origin) == 0


def test_media_comes_only_from_metas_host(hub_dir, meta, started):
    conn = wa_conn(hub_dir)
    doc = {"from": SENDER, "id": "wamid.doc", "timestamp": str(int(time.time())), "type": "document",
           "document": {"id": "media-1", "mime_type": "application/pdf", "filename": "invoice.pdf",
                        "caption": "Invoice"}}
    post(conn, payload(doc))
    route_all(hub_dir)
    from hubzoid.intake import service

    [item] = items(hub_dir)
    full = service.view(service.require(hub_dir, item["id"]), full=True, hub_dir=hub_dir)
    assert full["attachments"][0]["state"] == "available" and full["attachments"][0]["filename"] == "invoice.pdf"
    meta.media_url = "https://attacker.example/steal"
    event = admission.event_of(hub_dir, store.list_deliveries(hub_dir, "desk", conn.id)["items"][0])
    with pytest.raises(ChannelError) as exc:
        conn.adapter.fetch_file(conn, event, event.files[0])
    assert exc.value.code == "attachment_blocked"
    assert not any("attacker.example" in c for c in meta.calls)


# -- replies --------------------------------------------------------------------------------------

def _released(hub_dir, meta, ts=None):
    from hubzoid.access import store_for
    from hubzoid.intake import service
    from hubzoid.work_items import store as wstore

    conn = wa_conn(hub_dir)
    post(conn, payload(text_msg(ts=ts)))
    route_all(hub_dir)
    [item] = items(hub_dir)
    store_for(hub_dir).grant("ops@desk.example", "desk", "workflows_manage", actor="test")
    out = service.release(hub_dir, item["id"], actor="ops@desk.example", reason="known supplier",
                          expected_revision=item["revision"])
    return conn, wstore.get_item(hub_dir, out["work_item_id"])


def _post(hub_dir, work, text="Thanks, received. We will confirm by Friday."):
    from hubzoid import db
    from hubzoid.channels import thread
    from hubzoid.workflows import context

    with context.run_scope(hub="desk", workflow="requests", hub_dir=hub_dir, engine=db.operational_engine(hub_dir),
                           run_id="run-1", work={"item": work, "stage": None}):
        return thread.for_current_run().post(text, key="answer", kind="result")


def _mode(hub_dir, mode):
    (hub_dir / "work-items.yaml").write_text(
        f"version: 1\nwrite_modes:\n  customer_reply.send: {mode}\n")


def test_draft_is_refused_and_doctor_says_so(hub_dir, meta, started):
    from hubzoid.channels import doctor

    conn, work = _released(hub_dir, meta)
    _mode(hub_dir, "draft")
    with pytest.raises(Exception) as exc:
        _post(hub_dir, work)
    assert "draft_unsupported" in str(exc.value) or "draft" in str(exc.value)
    checks = {c["id"]: c for c in doctor.run(config.active(hub_dir, conn.id))["checks"]}
    assert checks["whatsapp:send_mode"]["code"] == "draft_unsupported" and meta.sent == []


def test_progress_never_reaches_the_sender(hub_dir, meta, started):
    from hubzoid import db
    from hubzoid.channels import thread
    from hubzoid.workflows import context

    conn, work = _released(hub_dir, meta)
    _mode(hub_dir, "approve")
    with context.run_scope(hub="desk", workflow="requests", hub_dir=hub_dir, engine=db.operational_engine(hub_dir),
                           run_id="run-1", work={"item": work, "stage": None}):
        assert thread.for_current_run().post("Working on it", key="p1", kind="progress") == []
    assert meta.sent == []


def test_approved_reply_goes_to_the_sender_once(hub_dir, meta, started):
    from hubzoid.channels import replies
    from hubzoid.work_items import store as wstore
    from hubzoid.work_items.errors import Parked

    conn, work = _released(hub_dir, meta)
    _mode(hub_dir, "approve")
    with pytest.raises(Parked) as parked:
        _post(hub_dir, work)
    op = wstore.get_op(hub_dir, parked.value.operation_id)
    assert op["requester"] == "ops@desk.example" and meta.sent == []
    wstore.update_op(hub_dir, op["id"], approval_json={"digest": op["args_digest"], "approver": "ops@desk.example"})
    _post(hub_dir, work)
    _post(hub_dir, work)
    assert len(meta.sent) == 1 and meta.sent[0]["to"] == SENDER and meta.sent[0]["type"] == "text"
    post(conn, payload(statuses=[{"id": "wamid.out1", "status": "delivered", "timestamp": "5",
                                  "recipient_id": SENDER}]))
    [reply] = [r for r in replies.outbox(hub_dir, "desk", conn.id) if r["state"] == "succeeded"]
    assert reply["delivery"]["status"] == "delivered"


def test_outside_the_window_the_reply_is_held(hub_dir, meta, started):
    from hubzoid.work_items import store as wstore
    from hubzoid.work_items.errors import NotSent, Parked

    conn, work = _released(hub_dir, meta, ts=time.time() - 25 * 3600)
    _mode(hub_dir, "approve")
    with pytest.raises(Parked) as parked:
        _post(hub_dir, work)
    op = wstore.get_op(hub_dir, parked.value.operation_id)
    wstore.update_op(hub_dir, op["id"], approval_json={"digest": op["args_digest"], "approver": "ops@desk.example"})
    with pytest.raises(NotSent) as exc:
        _post(hub_dir, work)
    assert "window_closed" in str(exc.value) and meta.sent == []


def test_timeout_after_sending_stays_uncertain(hub_dir, meta, started, monkeypatch):
    from hubzoid.work_items import store as wstore
    from hubzoid.work_items.errors import Parked

    conn, work = _released(hub_dir, meta)
    _mode(hub_dir, "approve")
    with pytest.raises(Parked) as parked:
        _post(hub_dir, work)
    op = wstore.get_op(hub_dir, parked.value.operation_id)
    wstore.update_op(hub_dir, op["id"], approval_json={"digest": op["args_digest"], "approver": "ops@desk.example"})
    meta.fail = 503
    with pytest.raises(Parked) as again:
        _post(hub_dir, work)
    assert wstore.get_op(hub_dir, again.value.operation_id)["state"] == "ambiguous"
    _post_count = len(meta.sent)
    with pytest.raises(Parked):
        _post(hub_dir, work)                     # never sent again on its own
    assert len(meta.sent) == _post_count == 0


def test_older_listener_steps_aside(hub_dir):
    from hubzoid.inbound.harness import _whatsapp_channel_conflict

    assert _whatsapp_channel_conflict(hub_dir, PNID) is None
    wa_conn(hub_dir)
    assert _whatsapp_channel_conflict(hub_dir, PNID) == "whatsapp-desk"
    assert _whatsapp_channel_conflict(hub_dir, "123456789") is None



# -- review fixes ------------------------------------------------------------------------------

def test_window_only_moves_forward_and_ignores_other_numbers(hub_dir, meta, started):
    conn = wa_conn(hub_dir)
    now = time.time()
    post(conn, payload(text_msg("wamid.new", ts=now - 60)))
    for n in range(60):                                    # many delayed old messages
        post(conn, payload(text_msg(f"wamid.old{n}", ts=now - 3 * 86400)))
    post(conn, payload(text_msg("wamid.other", ts=now), pnid="999999999999"))
    origin = wa.WhatsAppAdapter()._origin(conn.settings, SENDER)
    assert abs(wa.last_inbound_at(conn, origin) - (now - 60)) < 2


def test_receipts_in_one_batch_keep_the_furthest_status(hub_dir, meta, started):
    conn = wa_conn(hub_dir)
    st = lambda s: {"id": "wamid.out9", "status": s, "timestamp": "1", "recipient_id": SENDER}  # noqa: E731
    post(conn, payload(statuses=[st("read"), st("sent")]))
    assert config.active(hub_dir, conn.id).state["delivery_receipts"]["wamid.out9"]["status"] == "read"


def test_unsupported_messages_are_kept_for_review(hub_dir, meta, started):
    conn = wa_conn(hub_dir)
    post(conn, payload({"from": SENDER, "id": "wamid.loc", "timestamp": str(int(time.time())), "type": "location",
                        "location": {"latitude": 1, "longitude": 2}}))
    route_all(hub_dir)
    [item] = items(hub_dir)
    assert item["state"] == "quarantined" and item["reason"] == "unsupported_message"
