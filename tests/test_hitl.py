"""Approvals and questions (hubzoid.hitl): the rules file, the gate and its
grants, deciding, and a chat that waits for a person and then continues."""
from __future__ import annotations

import time
import uuid

import pytest
from fastapi.testclient import TestClient

from hubzoid.hitl import config as cfglib
from hubzoid.hitl import service
from tests.chat_helpers import ORIGIN, as_people, build_app, make_hub, who

RULES = """version: 1
tools:
  reserve_stock: {ask: always}
  label_order: {ask: once, allow_always: true}
connectors:
  crm: {ask: writes}
"""


@pytest.fixture
def hub(tmp_path, monkeypatch):
    hub = make_hub(tmp_path, monkeypatch)
    from hubzoid import migrations
    from hubzoid.db import operational_engine

    migrations.upgrade(operational_engine(hub), "operational")
    return hub


def caller(name="ana", **kw):
    base = {"account_id": f"u_{name}", "email": f"{name}@example.org", "conversation_id": "c1"}
    base.update(kw)
    return service.Caller(**base)


# -- the rules file ---------------------------------------------------------------
def test_rules_file_validates_with_line_numbers():
    cfg = cfglib.parse(RULES)
    assert cfg.rule_for("reserve_stock").ask == "always"
    assert cfg.rule_for("anything_else").ask == "never"          # eve's default: never ask
    assert cfg.rule_for("crm__create_deal", "crm").ask == "writes"
    with pytest.raises(cfglib.ConfigError) as bad:
        cfglib.parse("version: 1\ntools:\n  x: {ask: sometimes}\n")
    assert bad.value.path == "/tools/x/ask" and bad.value.line == 3
    with pytest.raises(cfglib.ConfigError):
        cfglib.parse("version: 1\napprovers: {group: finance}\n")  # groups are not available yet


def test_a_broken_file_asks_for_everything(hub):
    (hub / "hitl.yaml").write_text("version: 2\n")
    assert cfglib.load(hub).rule_for("read_knowledge").ask == "always"


# -- the gate and grants ---------------------------------------------------------
def test_no_file_never_asks(hub):
    v = service.check(hub, hub.name, caller(), tool="reserve_stock", arguments={"q": 1})
    assert v.action == "allow"


def test_always_asks_and_an_approval_admits_exactly_that_call_once(hub):
    (hub / "hitl.yaml").write_text(RULES)
    args = {"sku": "SKU-12", "quantity": 4}
    v = service.check(hub, hub.name, caller(), tool="reserve_stock", arguments=args)
    assert v.action == "ask" and v.request["allow_chat"] is False
    again = service.check(hub, hub.name, caller(), tool="reserve_stock", arguments=args)
    assert again.request["id"] == v.request["id"]                 # the same waiting request, not a second
    req = service.decide(hub, hub.name, v.request["id"], account_id="u_ana", email="ana@example.org",
                         decision="approve", expected_revision=1, digest=v.request["digest"])
    assert req["status"] == "approved"
    other = service.check(hub, hub.name, caller(), tool="reserve_stock", arguments={"sku": "SKU-12", "quantity": 9})
    assert other.action == "ask"                                   # different arguments need their own approval
    assert service.check(hub, hub.name, caller(), tool="reserve_stock", arguments=args).action == "allow"
    assert service.check(hub, hub.name, caller(), tool="reserve_stock", arguments=args).action == "ask"


def test_approve_for_this_chat_and_always_allow(hub):
    (hub / "hitl.yaml").write_text(RULES)
    v = service.check(hub, hub.name, caller(), tool="label_order", arguments={"id": 1})
    assert service.with_actions(v.request, "u_ana", "ana@example.org")["allowed_actions"] == \
        ["approve", "approve_chat", "approve_always", "reject"]
    service.decide(hub, hub.name, v.request["id"], account_id="u_ana", email="ana@example.org",
                   decision="approve_chat")
    assert service.check(hub, hub.name, caller(), tool="label_order", arguments={"id": 1}).action == "allow"
    assert service.check(hub, hub.name, caller(), tool="label_order", arguments={"id": 2}).action == "allow"
    assert service.check(hub, hub.name, caller(conversation_id="c2"), tool="label_order",
                         arguments={"id": 3}).action == "ask"      # only in that chat
    w = service.check(hub, hub.name, caller(conversation_id="c3"), tool="label_order", arguments={"id": 4})
    service.decide(hub, hub.name, w.request["id"], account_id="u_ana", email="ana@example.org",
                   decision="approve_always")
    assert service.check(hub, hub.name, caller(conversation_id="c9"), tool="label_order",
                         arguments={"id": 5}).action == "allow"
    assert service.check(hub, hub.name, caller("ben", conversation_id="c9"), tool="label_order",
                         arguments={"id": 5}).action == "ask"      # always is per person


def test_mcp_writes_mode_trusts_the_read_only_hint(hub):
    (hub / "hitl.yaml").write_text(RULES)
    assert service.check(hub, hub.name, caller(), tool="crm__get", arguments={}, connector="crm",
                         read_only=True).action == "allow"
    assert service.check(hub, hub.name, caller(), tool="crm__create", arguments={}, connector="crm").action == "ask"


def test_unattended_runs_are_refused_not_parked(hub):
    (hub / "hitl.yaml").write_text(RULES)
    v = service.check(hub, hub.name, caller(present=False, origin="workflow"), tool="reserve_stock", arguments={})
    assert v.action == "refuse" and "unattended" in v.message


def test_only_the_requester_or_a_named_approver_decides(hub):
    (hub / "hitl.yaml").write_text(RULES)
    v = service.check(hub, hub.name, caller(), tool="reserve_stock", arguments={})
    with pytest.raises(service.RequestError) as other:
        service.decide(hub, hub.name, v.request["id"], account_id="u_ben", email="ben@example.org",
                       decision="approve")
    assert other.value.status == 404                               # not even visible to someone else
    with pytest.raises(service.RequestError) as stale:
        service.decide(hub, hub.name, v.request["id"], account_id="u_ana", email="ana@example.org",
                       decision="approve", expected_revision=7)
    assert stale.value.code == "revision_conflict"
    service.decide(hub, hub.name, v.request["id"], account_id="u_ana", email="ana@example.org",
                   decision="reject", note="Not this week")
    with pytest.raises(service.RequestError) as twice:
        service.decide(hub, hub.name, v.request["id"], account_id="u_ana", email="ana@example.org",
                       decision="approve")
    assert twice.value.code == "already_decided"
    (hub / "hitl.yaml").write_text(RULES + "approvers: [ben@example.org]\n")
    w = service.check(hub, hub.name, caller(), tool="reserve_stock", arguments={"n": 2})
    with pytest.raises(service.RequestError):
        service.decide(hub, hub.name, w.request["id"], account_id="u_ana", email="ana@example.org",
                       decision="approve")                         # the requester is not on the list
    assert service.decide(hub, hub.name, w.request["id"], account_id="u_ben", email="ben@example.org",
                          decision="approve")["status"] == "approved"


# -- a chat that waits and continues --------------------------------------------
def _h(headers=ORIGIN):
    return {**headers, "Idempotency-Key": str(uuid.uuid4())}


def _wait(client, run_id, headers=ORIGIN):
    end = time.time() + 20
    while time.time() < end:
        r = client.get(f"/api/chat/runs/{run_id}", headers=headers).json()
        if r["status"] in ("complete", "stopped", "interrupted", "failed"):
            return r
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def _send(client, cid, branch, text, headers=ORIGIN, parent=None):
    r = client.post(f"/api/conversations/{cid}/operations", headers=_h(headers), json={
        "kind": "send", "branch_id": branch["id"], "parent_id": parent,
        "content": [{"type": "text", "text": text}]})
    assert r.status_code == 202, r.text
    return r.json()


def _messages(client, cid, headers=ORIGIN):
    return client.get(f"/api/conversations/{cid}", headers=headers).json()["messages"]


def _text(m):
    return "".join(p.get("text", "") for p in m["content"] if p.get("type") == "text")


def _until(fn, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        got = fn()
        if got:
            return got
        time.sleep(0.05)
    raise AssertionError("timed out")


def test_chat_waits_for_approval_then_continues(hub):
    (hub / "hitl.yaml").write_text(RULES)
    with TestClient(build_app()) as client:
        agent = client.app.state.chat.model_label
        conv = client.post("/api/conversations", headers=ORIGIN, json={"agent": agent}).json()
        cid, branch = conv["conversation"]["id"], conv["branch"]
        out = _send(client, cid, branch, "Please reserve the stock")
        _wait(client, out["run_id"])
        msgs = _messages(client, cid)
        call = next(p for p in msgs[-1]["content"] if p.get("type") == "tool-call")
        assert call["result"]["status"] == "waiting" and call["result"]["kind"] == "approval"
        pending = client.get("/api/requests").json()
        assert pending["pending_count"] == 1
        req = pending["items"][0]
        assert req["tool"] == "reserve_stock" and req["arguments"] == {"sku": "SKU-12", "quantity": 4}
        # automation never decides
        bot = client.post(f"/api/requests/{req['id']}/decisions",
                          headers={**ORIGIN, "Authorization": "Bearer k-chat-test"}, json={"decision": "approve"})
        assert bot.status_code == 403 and bot.json()["detail"]["code"] == "human_action_required"
        ok = client.post(f"/api/requests/{req['id']}/decisions", headers=ORIGIN,
                         json={"decision": "approve", "expected_revision": 1, "digest": req["digest"]})
        assert ok.status_code == 200 and ok.json()["status"] == "approved"
        done = _until(lambda: (m := _messages(client, cid)) and len(m) == 4 and m[-1]["status"] == "complete" and m)
        assert done[2]["role"] == "user" and _text(done[2]) == "Approved"
        assert "Reserved 4 units of SKU-12." in _text(done[3])
        assert client.get(f"/api/requests/{req['id']}").json()["continued"] is True


def test_writing_instead_of_answering_cancels_the_wait(hub):
    (hub / "hitl.yaml").write_text(RULES)
    with TestClient(build_app()) as client:
        agent = client.app.state.chat.model_label
        conv = client.post("/api/conversations", headers=ORIGIN, json={"agent": agent}).json()
        cid, branch = conv["conversation"]["id"], conv["branch"]
        out = _send(client, cid, branch, "Please reserve the stock")
        _wait(client, out["run_id"])
        rid = client.get("/api/requests").json()["items"][0]["id"]
        _send(client, cid, branch, "Actually, never mind", parent=out["assistant_message_id"])
        assert client.get(f"/api/requests/{rid}").json()["status"] == "cancelled"


def test_question_answer_continues_the_chat(hub):
    with TestClient(build_app()) as client:
        agent = client.app.state.chat.model_label
        conv = client.post("/api/conversations", headers=ORIGIN, json={"agent": agent}).json()
        cid, branch = conv["conversation"]["id"], conv["branch"]
        out = _send(client, cid, branch, "askme where to ship")
        _wait(client, out["run_id"])
        req = client.get("/api/requests").json()["items"][0]
        assert req["kind"] == "question" and [o["label"] for o in req["options"]][0] == "North (Recommended)"
        bad = client.post(f"/api/requests/{req['id']}/decisions", headers=ORIGIN, json={"decision": "answer"})
        assert bad.status_code == 422
        client.post(f"/api/requests/{req['id']}/decisions", headers=ORIGIN,
                    json={"decision": "answer", "answer": "North"})
        done = _until(lambda: (m := _messages(client, cid)) and len(m) == 4 and m[-1]["status"] == "complete" and m)
        assert _text(done[2]) == "North"


def test_questions_can_be_turned_off(hub):
    (hub / "hitl.yaml").write_text("version: 1\nquestions: false\n")
    from hubzoid.hitl import tools

    assert tools.ask_question_tool(hub) is None


# -- channels ---------------------------------------------------------------------
def test_whatsapp_numbered_reply_answers_only_the_one_waiting_request(hub):
    from hubzoid.hitl import channels

    (hub / "hitl.yaml").write_text(RULES)
    wa = caller(origin="whatsapp", conversation_id="whatsapp-15550001")
    v = service.check(hub, hub.name, wa, tool="reserve_stock", arguments={"sku": "SKU-12"})
    assert "Reply 1 to approve or 2 to reject." in channels.describe(v.request)
    assert channels.answer_by_reply(hub, chat_id="whatsapp-15550001", email="ben@example.org", text="1",
                                    via="whatsapp") is None        # not the requester
    done = channels.answer_by_reply(hub, chat_id="whatsapp-15550001", email="ana@example.org", text="1",
                                    via="whatsapp")
    assert done["status"] == "approved" and done["decided_via"] == "whatsapp"
    assert channels.answer_by_reply(hub, chat_id="whatsapp-15550001", email="ana@example.org", text="1",
                                    via="whatsapp") is None        # a repeat answers nothing
    w = service.check(hub, hub.name, wa, tool="reserve_stock", arguments={"sku": "SKU-99"})
    assert channels.answer_by_reply(hub, chat_id="whatsapp-15550001", email="ana@example.org",
                                    text="what does it cost?", via="whatsapp") is None
    assert service.get(hub, hub.name, w.request["id"])["status"] == "cancelled"   # writing steers


def test_slack_button_is_honoured_only_for_the_requester_and_fresh_card(hub):
    from hubzoid.hitl import channels

    (hub / "hitl.yaml").write_text(RULES)
    v = service.check(hub, hub.name, caller(origin="slack", conversation_id="slack-C1-1.0"), tool="reserve_stock",
                      arguments={"sku": "SKU-12"})
    blocks = channels.slack_blocks(v.request)
    value = blocks[2]["elements"][0]["value"]
    with pytest.raises(service.RequestError):
        channels.slack_decide(hub, action_id="hz_hitl_approve", value=value, email=None)   # unlinked Slack user
    with pytest.raises(service.RequestError):
        channels.slack_decide(hub, action_id="hz_hitl_approve", value=value, email="ben@example.org")
    stale = value.replace("|1|", "|9|")
    with pytest.raises(service.RequestError) as exc:
        channels.slack_decide(hub, action_id="hz_hitl_approve", value=stale, email="ana@example.org")
    assert exc.value.code == "revision_conflict"
    assert channels.slack_decide(hub, action_id="hz_hitl_approve", value=value,
                                 email="ana@example.org")["status"] == "approved"


def test_each_plan_review_in_a_run_is_a_new_version_with_its_digest(hub):
    from hubzoid.workflows.context import _hitl_create

    def ask(digest):
        return _hitl_create({"hub_dir": str(hub), "requester": "owner@example.org", "kind": "approval",
                             "title": "Order plan", "approvers": ["requester"], "workflow_id": "wf-1",
                             "subject": {"name": "Order plan", "url": "/a", "digest": digest, "plan": True}})

    first, second = ask("sha256:aa"), ask("sha256:bb")
    assert (first["subject"]["version"], second["subject"]["version"]) == (1, 2)
    assert second["subject"]["digest"] == "sha256:bb" and first["id"] != second["id"]


def test_events_show_only_your_own_requests(hub):
    (hub / "hitl.yaml").write_text(RULES)
    service.check(hub, hub.name, caller(), tool="reserve_stock", arguments={"sku": "secret"})
    mine = service.events_after(hub, hub.name, 0, account_id="u_ana", email="ana@example.org")
    assert mine and service.events_after(hub, hub.name, 0, account_id="u_ben", email="ben@example.org") == []


def test_a_slack_card_never_offers_approve_for_arguments_it_cannot_show(hub):
    from hubzoid.hitl import channels

    (hub / "hitl.yaml").write_text(RULES)
    v = service.check(hub, hub.name, caller(), tool="reserve_stock", arguments={"note": "x" * 5000})
    blocks = channels.slack_blocks(v.request)
    assert not any(b["type"] == "actions" for b in blocks) and "Hubzoid" in blocks[-1]["text"]["text"]


def test_a_replayed_workflow_request_is_found_not_asked_twice(hub):
    from hubzoid.workflows.context import _hitl_create

    payload = {"hub_dir": str(hub), "requester": "owner@example.org", "kind": "approval", "title": "Plan",
               "approvers": ["requester"], "workflow_id": "wf-9",
               "subject": {"name": "Plan", "url": "/a", "digest": "sha256:cc", "plan": True}}
    first, replay = _hitl_create(dict(payload)), _hitl_create(dict(payload))
    assert replay["id"] == first["id"] and replay["subject"]["version"] == 1


def test_mcp_servers_called_directly_are_gated(hub):
    import asyncio
    from types import SimpleNamespace

    from hubzoid.hitl import gate

    (hub / "hitl.yaml").write_text("version: 1\nconnectors:\n  erp:\n    ask: always\n")
    calls = []

    class Server:
        name = "erp"

        async def list_tools(self):
            return [SimpleNamespace(name="create_order", annotations=None)]

        async def call_tool(self, tool_name, arguments=None):
            calls.append(tool_name)
            return "done"

    from hubzoid.access import Identity, identity_scope

    s = gate.guard_mcp_server(Server(), hub)
    with identity_scope(Identity.make("ana@example.org", surface="web")):
        out = asyncio.run(s.call_tool("create_order", {"qty": 1}))
    assert calls == [] and "waiting" in out.content[0].text
