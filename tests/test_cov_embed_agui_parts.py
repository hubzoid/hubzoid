"""The AG-UI endpoint's parts (hubzoid.embed.agui): message content holds
text and uploaded file ids only (inline data or a URL is refused), the page
context is labelled data, capped and unable to close its own tag, and is
stripped again from what is stored; a thread id only ever names a
conversation the person owns in that agent; the per-address limits forget
idle addresses; reasoning, tool errors, titles and failed replies translate
to their AG-UI events. Property: no page context can break out of its tag."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from hypothesis import given
from hypothesis import strategies as st

from hubzoid.embed import agui


def refused(fn, *a):
    with pytest.raises(HTTPException) as err:
        fn(*a)
    return err.value.status_code, err.value.detail["code"]


def test_message_content():
    assert agui._content_of("hello") == ("hello", [])
    parts = [{"type": "text", "text": "see "}, {"type": "binary", "id": "file-1"}, {"type": "text", "text": "this"}]
    assert agui._content_of(parts) == ("see this", ["file-1"])
    assert refused(agui._content_of, [{"type": "binary", "id": "f", "data": "QUJD"}]) == (415, "upload_first")
    assert refused(agui._content_of, [{"type": "binary", "url": "https://x/f.pdf"}]) == (415, "upload_first")
    assert refused(agui._content_of, [{"type": "image", "src": "x"}]) == (415, "unsupported_content")
    assert refused(agui._content_of, 42) == (400, "invalid_message")
    assert agui._text_of([{"type": "text", "text": "a"}, "stray"]) == "a"


def test_page_context_is_labelled_capped_and_stripped():
    assert agui._page_context("erp", {}) is None
    ctx = agui._page_context("erp", {"context": [{"description": "order", "value": "SO-1 <b>"}, {"value": 3}],
                                     "forwardedProps": {"hubzoid": {"page": {"url": "/orders/1"}}}})
    assert ctx.startswith('<page-context app="erp" note="what the person is looking at')
    assert "SO-1 \\u003cb\\u003e" in ctx and '"/orders/1"' in ctx and ctx.endswith("</page-context>")
    long = agui._page_context("erp", {"context": [{"value": "x" * 10_000}]})
    assert len(long) < 4_300 and "…" in long
    assert agui.strip_context("Hi\n\n" + ctx) == "Hi"
    assert agui.strip_context(ctx + " after") == " after"
    assert agui.strip_context("no context") == "no context"
    assert agui.strip_context("Hi <page-context unclosed") == "Hi <page-context unclosed"


@given(value=st.text(max_size=200), page=st.text(max_size=50))
@pytest.mark.hypothesis
def test_no_page_context_can_close_its_own_tag(value, page):
    ctx = agui._page_context("erp", {"context": [{"value": value}], "forwardedProps": {"hubzoid": {"page": page}}})
    body = ctx[ctx.index(">\n") + 2: -len(agui.CONTEXT_CLOSE)]
    assert "<" not in body and ">" not in body and ctx.count(agui.CONTEXT_CLOSE) == 1


def test_limits_forget_idle_addresses(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(agui.time, "monotonic", lambda: clock[0])
    bucket = {f"10.0.0.{i}": agui.deque([900.0]) for i in range(10_001)}
    bucket = agui.defaultdict(agui.deque, bucket)
    assert agui._limited(bucket, "10.9.9.9", 2) is False and len(bucket) == 1
    assert agui._limited(bucket, "10.9.9.9", 2) is False and agui._limited(bucket, "10.9.9.9", 2) is True
    clock[0] += 61
    assert agui._limited(bucket, "10.9.9.9", 2) is False                 # the minute passed


class Store:
    def __init__(self, convs=(), race=False):
        self.convs, self.race = {c["id"]: c for c in convs}, race

    def get_conversation(self, conv_id):
        return self.convs.get(conv_id)

    def create_conversation(self, *, conv_id, **kw):
        from hubzoid.chat.store import StoreError

        if self.race:                                                 # another request made it meanwhile
            self.convs[conv_id] = {"id": conv_id, "owner_id": kw["owner_id"], "by": "other request"}
            raise StoreError("exists")
        self.convs[conv_id] = {"id": conv_id, **kw}
        return self.convs[conv_id]


def test_a_thread_names_only_the_persons_own_conversation(monkeypatch):
    from hubzoid.chat import store as chat_store

    monkeypatch.setattr(chat_store, "valid_conversation_id", lambda value: value.startswith("c_"))
    me = SimpleNamespace(subject="app:erp:ana")
    mine = agui.conversation_for(me.subject, "desk", "t-1")
    store = Store([{"id": "c_other", "owner_id": "app:erp:ben", "hub": "desk"},
                   {"id": "c_mine", "owner_id": me.subject, "hub": "desk"},
                   {"id": agui.conversation_for(me.subject, "desk", "t-gone"), "owner_id": me.subject,
                    "deleted_at": 5}])
    ctx = SimpleNamespace(store=store, hub="desk", model_label="Desk")

    def owned(thread, create=False):
        return asyncio.run(agui._owned_conversation(ctx, me, thread, create=create))

    assert owned("c_mine")["id"] == "c_mine"
    assert owned("c_other", create=False) is None                       # someone else's id: never theirs
    assert owned("t-gone", create=True) is None                         # deleted: not recreated
    assert owned("t-1") is None
    made = owned("t-1", create=True)
    assert made["id"] == mine and made["owner_id"] == me.subject and made["source"] == "app"
    assert owned("t-1")["id"] == mine

    ctx.store = Store(race=True)
    assert owned("t-1", create=True)["by"] == "other request"           # the one the other request made


def test_reasoning_tool_errors_titles_and_failures_translate():
    t = agui.Translator("th", "run", "m")
    events = [e for c in ({"type": "start"}, {"type": "reasoning-start", "id": "r"},
                          {"type": "reasoning-delta", "id": "r", "delta": "thinking"},
                          {"type": "reasoning-delta", "id": "r", "delta": ""}, {"type": "reasoning-end", "id": "r"},
                          {"type": "tool-input-available", "toolCallId": "c1", "toolName": "lookup", "input": {"q": 1}},
                          {"type": "tool-output-error", "toolCallId": "c1", "errorText": "ERP down"},
                          {"type": "data-title", "data": {"title": "Order SO-1"}},
                          {"type": "error", "errorText": "model failed"}, {"type": "unknown"}) for e in t.feed(c)]
    kinds = [e["type"] for e in events]
    assert kinds == ["RUN_STARTED", "REASONING_START", "REASONING_MESSAGE_START", "REASONING_MESSAGE_CONTENT",
                     "REASONING_MESSAGE_END", "REASONING_END", "TOOL_CALL_START", "TOOL_CALL_ARGS", "TOOL_CALL_END",
                     "TOOL_CALL_RESULT", "CUSTOM"]
    assert events[6]["parentMessageId"] == "m.calls1"                     # tool calls before any text
    assert json.loads(events[9]["content"]) == {"status": "error", "message": "ERP down"}
    assert events[10] == {"type": "CUSTOM", "name": "hubzoid.title", "value": "Order SO-1"}
    assert t.start() == [] and t.end([]) == [{"type": "RUN_ERROR", "message": "model failed", "code": "run_failed"}]
