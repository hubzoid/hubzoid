"""Approval error paths, channel replies, tool gating and HTTP visibility."""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hubzoid import _request_ctx
from hubzoid.access.identity import Identity, identity_scope
from hubzoid.hitl import channels, gate, routes, service, store, tools
from tests.test_hitl import RULES, caller, hub  # noqa: F401


def question(hub, **kw):
    return service.create(hub, hub.name, caller(), kind="question", title="Where?", question="Where?",
        options=[{"label": "Here (Recommended)", "description": "Near"}, {"label": "There"}], **kw)


def approval(hub, **kw):
    return service.create(hub, hub.name, caller(), kind="approval", title="Do work", tool="work", **kw)


@pytest.mark.parametrize("kind", ["unexpected", ""])
def test_invalid_request_kind(hub, kind):
    with pytest.raises(service.RequestError) as exc:
        service.create(hub, hub.name, caller(), kind=kind, title="Work")
    assert exc.value.code == "validation_failed" and service.list_for(hub, hub.name, "u_ana", "ana@example.org") == []


@pytest.mark.parametrize("kw,code", [({"decision": "bad"}, "validation_failed"),
    ({"decision": "approve", "note": "x" * 4001}, "validation_failed"),
    ({"decision": "approve", "digest": "wrong"}, "digest_mismatch"),
    ({"decision": "approve_always"}, "validation_failed")])
def test_invalid_decision_keeps_pending(hub, kw, code):
    req = approval(hub)
    with pytest.raises(service.RequestError) as exc:
        service.decide(hub, hub.name, req["id"], account_id="u_ana", email="ana@example.org", **kw)
    assert exc.value.code == code and service.get(hub, hub.name, req["id"])["status"] == "pending"


def test_question_limits_and_recommended_label(hub):
    req = question(hub)
    for answer in ("", "x" * (service.MAX_ANSWER + 1)):
        with pytest.raises(service.RequestError) as exc:
            service.decide(hub, hub.name, req["id"], account_id="u_ana", email="ana@example.org", decision="answer", answer=answer)
        assert exc.value.code == "validation_failed"
    done = service.decide(hub, hub.name, req["id"], account_id="u_ana", email="ana@example.org", decision="answer", answer="Here (Recommended)")
    assert done["answer"] == "Here" and done["allowed_actions"] == []
    assert service.visible_outcome(done) == "Here"
    assert 'Answer to your question "Where?"' in service.outcome_text(done)


def test_auto_check_failure_and_oversize_are_safe(hub):
    (hub / "hitl.yaml").write_text("version: 1\ntools: {work: auto}\n")
    assert service.check(hub, hub.name, caller(), tool="work", arguments={}, auto_check=lambda *a: True).action == "allow"
    def broken(*a):
        raise RuntimeError("offline")
    v = service.check(hub, hub.name, caller(), tool="work", arguments={}, auto_check=broken)
    assert v.action == "ask" and v.request["tool"] == "work"
    v = service.check(hub, hub.name, caller(), tool="work", arguments={"body": "x" * service.MAX_ARGS_BYTES})
    assert v.action == "refuse" and "too large" in v.message


def test_plan_note_required_and_outcome(hub):
    req = approval(hub, subject={"plan": "exact plan"})
    with pytest.raises(service.RequestError) as exc:
        service.decide(hub, hub.name, req["id"], account_id="u_ana", email="ana@example.org", decision="request_changes")
    assert exc.value.code == "validation_failed"
    done = service.decide(hub, hub.name, req["id"], account_id="u_ana", email="ana@example.org", decision="request_changes", note="Change scope")
    assert service.visible_outcome(done) == "Changes requested: Change scope"
    assert "Plan not approved" in service.outcome_text(done) and "Change scope" in service.outcome_text(done)
    assert "Plan approved" in service.outcome_text({**done, "status": "approved"})


def test_channel_question_reply_and_idempotent_thread(hub):
    req = question(hub, workflow_id="wf")
    assert "1. Here (Recommended) (Near)" in channels.describe(req)
    assert channels.answer_by_reply(hub, chat_id="c1", email="ana@example.org", text=" ", via="whatsapp") is None
    done = channels.answer_by_reply(hub, chat_id="c1", email="ana@example.org", text="2", via="whatsapp")
    assert done["answer"] == "There"
    assert channels.answer_in_thread(hub, request_id=req["id"], email="ana@example.org", text="new", via="whatsapp") == "answered"
    assert channels.answer_in_thread(hub, request_id=req["id"], email="ben@example.org", text="new", via="whatsapp") == "taken"
    assert channels.answer_in_thread(hub, request_id="missing", email="ana@example.org", text="yes", via="slack") == "taken"
    assert channels.pending_questions_for_run(hub, "", "ana@example.org") == []


def test_multiple_channel_requests_cancel_on_ordinary_message(hub):
    a = approval(hub, arguments={"a": 1})
    b = approval(hub, arguments={"a": 2})
    assert channels.answer_by_reply(hub, chat_id="c1", email="ana@example.org", text="1", via="whatsapp") is None
    assert len(channels.pending_in_chat(hub, "c1", "ana@example.org", since=0)) == 2
    assert channels.answer_by_reply(hub, chat_id="c1", email="ana@example.org", text="Changed my mind", via="whatsapp") is None
    assert service.get(hub, hub.name, a["id"])["status"] == "cancelled"
    assert service.get(hub, hub.name, b["id"])["status"] == "cancelled"


def test_slack_questions_and_large_approval(hub):
    req = question(hub)
    blocks = channels.slack_blocks(req)
    button = blocks[1]["elements"][0]
    assert button["text"]["text"] == "Here (Recommended)"
    done = channels.slack_decide(hub, action_id=button["action_id"], value=button["value"], email="ana@example.org")
    assert done["answer"] == "Here" and done["decided_via"] == "slack"
    blocks = channels.slack_blocks({**approval(hub), "arguments": {"body": "x" * 3000}})
    assert all(b["type"] != "actions" for b in blocks)
    assert "too long" in blocks[1]["text"]["text"]


@pytest.mark.parametrize("email,value,action,code", [(None, "bad", "approve", "human_action_required"),
    ("ana@example.org", "bad", "approve", "invalid_request"),
    ("ana@example.org", "missing|1|hash", "approve", "not_found")])
def test_slack_refuses_unverified_and_bad_cards(hub, email, value, action, code):
    with pytest.raises(service.RequestError) as exc:
        channels.slack_decide(hub, action_id="hz_hitl_" + action, value=value, email=email)
    assert exc.value.code == code


def test_slack_bad_option_and_action(hub):
    req = question(hub)
    ref = f"{req['id']}|1|{req['digest']}"
    for action, value, code in (("answer_9", ref + "|9", "validation_failed"),
                                ("unknown", ref, "invalid_request")):
        with pytest.raises(service.RequestError) as exc:
            channels.slack_decide(hub, action_id="hz_hitl_" + action, value=value, email="ana@example.org")
        assert exc.value.code == code


def test_thread_question_and_plan_decisions(hub):
    req = question(hub, workflow_id="wf")
    assert channels.pending_questions_for_run(hub, "wf", "ana@example.org")[0]["id"] == req["id"]
    assert channels.answer_in_thread(hub, request_id=req["id"], email="ana@example.org", text=" ", via="linear") == "taken"
    assert channels.answer_in_thread(hub, request_id=req["id"], email="ana@example.org", text="1", via="linear") == "answered"
    plan = approval(hub, subject={"plan": "exact"}, workflow_id="wf")
    assert channels.answer_in_thread(hub, request_id=plan["id"], email="ana@example.org", text="approve", via="linear") == "taken"
    assert channels.answer_in_thread(hub, request_id=plan["id"], email="ana@example.org", text="looks good", via="linear", approvals=True) == "taken"
    assert channels.answer_in_thread(hub, request_id=plan["id"], email="ana@example.org", text="changes: narrow scope", via="linear", approvals=True) == "answered"
    assert service.get(hub, hub.name, plan["id"])["note"] == "narrow scope"
    assert channels.answer_in_thread(hub, request_id=plan["id"], email="ana@example.org", text="approve", via="linear", approvals=True) == "answered"


@pytest.mark.parametrize("raw,message", [({}, "question is required"),
    ({"question": "Where", "options": [{}]}, "offer 2 or 3"),
    ({"question": "Where", "options": [{"label": ""}, {"label": "ok"}]}, "every option")])
def test_question_tool_validation(raw, message):
    with pytest.raises(ValueError, match=message):
        tools._validate(raw)


def test_question_tool_unavailable_and_disabled(hub):
    tool = tools.ask_question_tool(hub)
    assert json.loads(asyncio.run(tool.on_invoke_tool(None, '{}')))["status"] == "error"
    assert json.loads(asyncio.run(tool.on_invoke_tool(None, '{"question":"Where?"}')))["status"] == "unavailable"
    (hub / "hitl.yaml").write_text("version: 1\nquestions: false\n")
    assert tools.ask_question_tool(hub) is None


def test_gate_context_and_waiting_cannot_run_more_tools(hub, monkeypatch):
    slot = SimpleNamespace(hitl={"account_id": "u_ana", "email": "ana@example.org"})
    monkeypatch.setattr(_request_ctx, "native_session", lambda: slot)
    assert gate.caller().account_id == "u_ana"
    gate.note_waiting("work", {"id": "r", "kind": "approval"})
    assert gate.suspended() and slot.hitl_waiting == [{"tool": "work", "request_id": "r", "kind": "approval"}]
    verdict = gate.evaluate(hub, "work", {})
    assert verdict.action == "refuse" and "Stop here" in verdict.message
    assert gate._arguments("not-json") == {"input": "not-json"}
    assert gate._arguments("") == {} and gate._arguments([1]) == [1]


def test_gate_surface_identity_and_auto_check(hub, monkeypatch):
    monkeypatch.setattr(_request_ctx, "native_session", lambda: None)
    with identity_scope(Identity.make("ana@example.org", surface="workflow")):
        assert gate.caller().present is False
    with identity_scope(Identity.make("ana@example.org", surface="mcp", account_id="u_ana")):
        assert gate.caller().conversation_id is None and gate.caller().origin == "mcp"
    from hubzoid import jev
    monkeypatch.setattr(jev, "call", lambda *a, **kw: {"answers": {"permission": {"choice": "clear"}}})
    assert gate._auto_check(hub)("read", {}) is True


def test_http_requests_and_configuration(hub, monkeypatch):
    from hubzoid.control import authz
    async def who(request):
        return SimpleNamespace(id="u_ana", email="ana@example.org", via_key=False)
    async def manager(*a, **kw):
        return SimpleNamespace()
    monkeypatch.setattr(authz, "require_request", manager)
    app = FastAPI()
    routes.mount(app, SimpleNamespace(hub_dir=hub, hub=hub.name, user=who, mutation=who))
    c = TestClient(app)
    req = question(hub)
    assert c.get("/api/requests").json()["pending_count"] == 1
    assert c.get("/api/requests?status=all&conversation_id=c1").json()["items"][0]["id"] == req["id"]
    assert c.get("/api/requests/events?after=bad").status_code == 400
    assert c.get("/api/requests/missing").status_code == 404
    assert c.post("/api/requests/missing/cancel", json={}).status_code == 404
    assert c.post(f"/api/requests/{req['id']}/decisions", json={"decision": "bad"}).status_code == 422
    assert c.post(f"/api/requests/{req['id']}/cancel", json={}).json()["status"] == "cancelled"
    assert c.post(f"/api/requests/{req['id']}/cancel", json={}).json()["status"] == "cancelled"
    assert c.get("/api/hitl/config").json()["revision"] == "absent"
    assert c.post("/api/hitl/config/validate", json={"text": "version: 2"}).json()["valid"] is False
    assert c.post("/api/hitl/config/validate", json={"text": RULES}).json()["valid"] is True
    # In-app editing is a Beta switch, off by default: the write is refused.
    off = c.put("/api/hitl/config", json={"text": RULES, "expected_revision": "absent"})
    assert off.status_code == 409 and off.json()["detail"]["code"] == "in_app_editing_off"
    assert c.get("/api/hitl/config").json()["exists"] is False
    monkeypatch.setenv("HUBZOID_IN_APP_EDITING", "true")
    assert c.put("/api/hitl/config", json={"text": RULES, "expected_revision": "absent"}).status_code == 200
    assert c.get("/api/hitl/config").json()["exists"] is True
    assert c.put("/api/hitl/config", json={"text": RULES, "expected_revision": "absent"}).status_code == 409
    assert c.put("/api/hitl/config", json={"text": "version: 2", "expected_revision": "absent"}).status_code == 422


def test_lost_continuation_claim_is_released_once(hub):
    req = approval(hub)
    done = service.decide(hub, hub.name, req["id"], account_id="u_ana", email="ana@example.org", decision="reject")
    assert done["status"] == "rejected" and service.mark_continued(hub, req["id"])
    with service.engine(hub).begin() as conn:
        conn.execute(store.requests.update().where(store.requests.c.id == req["id"]).values(decided_at=time.time() - 120))
    assert routes.release_lost_continuations(hub, hub.name) == 1
    assert routes.release_lost_continuations(hub, hub.name) == 0
    assert service.get(hub, hub.name, req["id"])["continued"] is False
