"""A workflow started from chat reports back to that chat: its later question
as a card, its result as a message, once each and never over a reply in
progress (hubzoid.workflows.chat_link, ChatEngine.post_from_run)."""
from __future__ import annotations

import time

import pytest

from tests.chat_helpers import make_hub


@pytest.fixture
def chat(tmp_path, monkeypatch):
    hub = make_hub(tmp_path, monkeypatch)
    from hubzoid import migrations
    from hubzoid.chat import store as store_mod
    from hubzoid.chat.engine import Actor, ChatEngine
    from hubzoid.db import operational_engine

    migrations.upgrade(operational_engine(hub), "operational")
    engine = ChatEngine(hub, store_mod.for_hub(hub), hub.name.lower())
    actor = Actor(id="u_ana", email="ana@example.org")
    conv = engine.create_conversation(actor, agent="Test")
    return hub, engine, actor, {"conversation_id": conv["conversation_id"], "branch_id": conv["branch"]["id"],
                                "account_id": "u_ana"}


def _texts(engine, cid):
    from hubzoid.chat.engine import messages

    with engine.engine.connect() as c:
        rows = c.execute(messages.select().where(messages.c.conversation_id == cid)).fetchall()
    return [r.text for r in rows]


def test_a_late_result_comes_back_once(chat):
    from hubzoid.workflows import chat_link

    hub, engine, _actor, link = chat
    env = {"chat": link, "started_at": time.time() - 999}
    for _ in range(2):  # a replayed run posts nothing new
        chat_link.post_result(hub, env, "launch_plan_review", "run-1", ok=True,
                              result={"approved": True, "region": "India", "note": None})
    texts = _texts(engine, link["conversation_id"])
    assert len(texts) == 1 and "Launch plan review finished." in texts[0] and "Region: India" in texts[0]
    assert "Note" not in texts[0]


def test_a_quick_result_is_left_to_the_reply_that_waited(chat):
    from hubzoid.workflows import chat_link

    hub, engine, _actor, link = chat
    chat_link.post_result(hub, {"chat": link, "started_at": time.time()}, "report", "run-2", ok=True,
                          result="done", tool_wait=60)
    assert _texts(engine, link["conversation_id"]) == []


def test_a_post_waits_for_a_reply_in_progress_and_skips_a_card_already_shown(chat):
    hub, engine, actor, link = chat
    cid, branch = link["conversation_id"], link["branch_id"]
    parts = [{"type": "text", "text": "x"}]
    assert engine.post_from_run(cid, branch_id=branch, actor_id="u_ana", message_id="m_req_aaaaaaaa",
                                parts=parts) == "posted"
    assert engine.post_from_run(cid, branch_id=branch, actor_id="u_ana", message_id="m_req_aaaaaaaa",
                                parts=parts) == "exists"
    assert engine.post_from_run(cid, branch_id=branch, actor_id="u_ana", message_id="m_req_bbbbbbbb",
                                parts=parts, unless_mentioned="x") == "exists"
    assert engine.post_from_run(cid, branch_id="nope", actor_id="u_ana", message_id="m_req_cccccccc",
                                parts=parts) == "gone"


def test_an_answer_posted_by_a_run_notifies_like_a_reply(chat):
    from hubzoid.chat import feedback
    from hubzoid.workflows import chat_link

    hub, engine, _actor, link = chat
    chat_link.post_result(hub, {"chat": link, "started_at": time.time() - 999}, "repeated_request", "run-9",
                          ok=True, result="3 items are low")
    with engine.engine.connect() as c:
        rows = c.execute(feedback.inbox.select().where(feedback.inbox.c.recipient_id == "u_ana")).fetchall()
    assert len(rows) == 1
