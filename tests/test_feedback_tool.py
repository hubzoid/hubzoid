"""record_feedback (hubzoid.tools.feedback_tool): offered only when turned on,
the same tool on every runtime, and bound to the run, never to what the
model says.

The chat turns run through the real bridge with the scripted runtime, whose
``feedback`` keyword calls the real tool inside the real run: the request's
first sentence is the person's quote, the rest their comment."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

from hubzoid.chat import feedback as fb
from hubzoid.tools import feedback_tool
from tests.chat_helpers import ORIGIN, build_app, make_hub
from tests.test_chat_operations import new_conv, op, send, snap, wait_run

ASK = "Save negative feedback on your previous answer. The price was outdated."


@pytest.fixture
def hub(tmp_path, monkeypatch):
    return make_hub(tmp_path, monkeypatch)


@pytest.fixture
def client(hub):
    with TestClient(build_app()) as c:
        yield c


def turn_on(hub, **values):
    from hubzoid.chat import config as cfg

    svc = cfg.service_for(hub)
    active = svc.active()
    doc = active.document
    doc["feedback"].update({"tool": True, **values})
    svc.apply(doc, expected_revision=active.revision or None, actor="t")


def turn(client, cid, text, parent=None, branch_id=None):
    s = snap(client, cid)
    b = next(x for x in s["branches"] if x["id"] == (branch_id or s["view"]["branch_id"]))
    out = send(client, cid, text, b, parent=parent if parent is not None else b["head_id"])
    run = wait_run(client, out["run_id"])
    return out, "".join(p.get("text", "") for p in run["message"]["content"] if p.get("type") == "text")


def ratings(client):
    with client.app.state.chat.engine.engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(sa.select(fb.feedback).where(fb.feedback.c.deleted_at.is_(None)))]


# -- offered, and the same on every runtime --------------------------------------------------

def test_offered_only_when_turned_on(hub):
    ctx = SimpleNamespace(hub_dir=hub)
    assert feedback_tool.make(ctx) == []           # off by default: web feedback defaults unchanged
    turn_on(hub)
    [tool] = feedback_tool.make(ctx)
    assert tool.name == "record_feedback" and tool.params_json_schema["required"] == ["rating", "quote"]
    assert tool.params_json_schema["additionalProperties"] is False
    turn_on(hub, enabled=False)                     # collection off: the tool goes with it
    assert feedback_tool.make(ctx) == []
    from hubzoid import mcp_server

    assert "record_feedback" in mcp_server.CHAT_SCOPED_TOOLS   # never on the MCP surface


@pytest.mark.asyncio
async def test_same_schema_and_result_on_claude_and_codex(hub):
    """One FunctionTool, adapted: the same name, schema and result text."""
    from hubzoid.factory_claude import _to_claude_tool
    from hubzoid.factory_codex import CodexRuntime
    from tests.test_codex_runtime import Process, events

    turn_on(hub)
    [tool] = feedback_tool.make(SimpleNamespace(hub_dir=hub))
    args = {"rating": "positive", "quote": "save it"}
    direct = await tool.on_invoke_tool(None, json.dumps(args))
    assert json.loads(direct)["status"] == "denied"     # no chat run here: nothing is saved
    claude = _to_claude_tool(tool)
    assert (claude.name, claude.description, claude.input_schema) == (tool.name, tool.description,
                                                                     tool.params_json_schema)
    via_claude = await claude.handler(args)
    assert via_claude["content"][0]["text"] == direct
    proc = Process(events({"method": "item/tool/call", "id": 10, "params": {
        "tool": "record_feedback", "arguments": args, "callId": "c", "threadId": "thread"}}))
    rt = CodexRuntime(name="demo", instructions="x", registry={tool.name: tool}, tool_mode="off")
    _ = [x async for x in rt._exchange(proc, "hi", str(hub), {})]
    start = next(m["params"] for m in proc.messages if m.get("method") == "thread/start")
    assert start["dynamicTools"] == [{"type": "function", "name": tool.name, "description": tool.description,
                                      "inputSchema": tool.params_json_schema}]
    reply = next(m for m in proc.messages if m.get("id") == 10)["result"]["contentItems"][0]["text"]
    assert reply == direct


# -- bound to the run -----------------------------------------------------------------------

def test_saves_the_previous_answer_with_the_persons_words(client, hub):
    turn_on(hub)
    cid, _ = new_conv(client)
    first, _ = turn(client, cid, "What does the starter plan cost?")
    second, _ = turn(client, cid, "And the team plan?")
    asked, said = turn(client, cid, ASK)
    assert said.startswith("Saved: negative feedback")
    [row] = ratings(client)
    meta = json.loads(row["source_metadata"])
    owner = snap(client, cid)["conversation"]["owner_id"]
    assert row["message_id"] == second["assistant_message_id"]          # the previous answer, not the first
    assert (row["source"], row["author_id"], row["rating"]) == ("tool", owner, "negative")
    assert row["comment"] == "The price was outdated."
    assert meta["inbound_message_id"] == asked["user_message_id"]      # the person's own message
    assert meta["quote"] == "Save negative feedback on your previous answer"
    assert meta["reply_message_id"] == asked["assistant_message_id"]
    # The next request skips the reply that only confirmed the rating, and updates the same record.
    _, again = turn(client, cid, "Save positive feedback on your previous answer")
    [row2] = ratings(client)
    assert row2["id"] == row["id"] and row2["message_id"] == second["assistant_message_id"]
    assert row2["rating"] == "positive" and "it was negative" in again
    item = client.get("/api/chat/feedback").json()["items"][0]
    assert item["source"] == "tool" and item["quote"] == "Save positive feedback on your previous answer"
    assert first["assistant_message_id"] not in {r["message_id"] for r in ratings(client)}


def test_nothing_to_rate_and_no_deployment_wide_last_answer(client, hub):
    turn_on(hub)
    other, _ = new_conv(client)
    elsewhere, _ = turn(client, other, "An answer in another conversation")
    cid, _ = new_conv(client)
    _, said = turn(client, cid, ASK)      # the first message here: there is no previous answer
    assert "no such earlier answer" in said.lower() and ratings(client) == []
    _, said = turn(client, cid, "Save positive feedback on your previous answer")
    # The previous answer here is the "nothing saved" reply, never the other conversation's answer.
    [row] = ratings(client)
    assert row["conversation_id"] == cid and row["message_id"] != elsewhere["assistant_message_id"]


def test_follows_the_branch_being_viewed(client, hub):
    turn_on(hub)
    cid, _ = new_conv(client)
    turn(client, cid, "first question")
    second, _ = turn(client, cid, "second question")
    regen = op(client, cid, {"kind": "regenerate", "target_message_id": second["user_message_id"]})
    assert regen.status_code == 202, regen.text
    wait_run(client, regen.json()["run_id"])
    alt = regen.json()["assistant_message_id"] if "assistant_message_id" in regen.json() else \
        next(m["id"] for m in snap(client, cid)["messages"]
             if m["role"] == "assistant" and m["parent_id"] == second["user_message_id"]
             and m["id"] != second["assistant_message_id"])
    turn(client, cid, ASK, parent=alt, branch_id=regen.json()["branch_id"])
    [row] = ratings(client)
    assert row["message_id"] == alt and row["message_id"] != second["assistant_message_id"]


def test_model_cannot_choose_author_target_or_words(client, hub):
    """Extra arguments are ignored, quotes must be the person's, and a retry changes nothing."""
    from hubzoid import _request_ctx
    from hubzoid.access import Identity, identity_scope

    turn_on(hub)
    cid, _ = new_conv(client)
    answer, _ = turn(client, cid, "What is the price?")
    asked, _ = turn(client, cid, "Please note this. It was wrong.")      # no request to save anything
    owner = snap(client, cid)["conversation"]["owner_id"]
    slot = _request_ctx.SessionSlot()
    slot.hitl = {"account_id": owner, "email": "owner@x", "present": True, "origin": "chat",
                 "conversation_id": cid, "branch_id": snap(client, cid)["view"]["branch_id"],
                 "message_id": asked["assistant_message_id"], "run_id": "r"}

    def call(args):
        with _request_ctx.native_session_scope(slot), identity_scope(Identity.make("owner@x", account_id=owner,
                                                                                   surface="web")):
            return json.loads(feedback_tool.record(hub, args))

    spoof = {"rating": "negative", "quote": "Please note this", "author_id": "u_ben", "conversation_id": "x",
             "message_id": "someone-elses", "answer_id": "someone-elses"}
    invented = call({**spoof, "quote": "Save negative feedback"})
    assert invented["status"] == "denied" and ratings(client) == []      # words the person never wrote
    assert call({**spoof, "comment": "The price was outdated"})["status"] == "denied"
    first = call(spoof)
    assert first["status"] == "saved"
    [row] = ratings(client)
    assert (row["author_id"], row["message_id"], row["conversation_id"]) == (owner, answer["assistant_message_id"],
                                                                             cid)
    assert call(spoof)["status"] == "unchanged" and ratings(client)[0]["revision"] == row["revision"]
    slot.hitl["origin"] = "api"           # an API key run: nobody in the chat to have asked
    assert call(spoof)["status"] == "denied"


def test_turning_it_off_stops_the_next_call(client, hub):
    turn_on(hub)
    [tool] = feedback_tool.make(SimpleNamespace(hub_dir=hub))   # built while on
    turn_on(hub, tool=False)
    out = json.loads(asyncio.run(tool.on_invoke_tool(None, json.dumps({"rating": "positive", "quote": "x y z"}))))
    assert out["status"] == "denied" and "turned off" in out["message"]
    cid, _ = new_conv(client)
    turn(client, cid, "An answer")
    _, said = turn(client, cid, ASK)
    assert "can't save feedback" in said and ratings(client) == []


def test_config_change_rebuilds_the_runtime_for_the_next_turn(client, hub):
    from hubzoid.chat import config as cfg

    before = client.app.state.chat.runtime
    active = cfg.service_for(hub).active()
    doc = active.document
    doc["feedback"]["tool"] = True
    r = client.put("/api/chat/config", headers=ORIGIN, json={"config": doc,
                                                             "expected_revision": active.revision or "absent"})
    assert r.status_code == 200, r.text
    assert r.json()["runtime_reloaded"] is True and client.app.state.chat.runtime is not before
