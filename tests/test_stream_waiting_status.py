"""The chat stream shows a waiting status until the first content arrives."""
from __future__ import annotations

import asyncio
import json

import pytest

from hubzoid import server


class _Runtime:
    def __init__(self, parts=(), error=None):
        self.parts, self.error = list(parts), error

    async def stream(self, prompt):
        for part in self.parts:
            yield part
        if self.error:
            raise self.error


def _events(rt):
    async def collect():
        out = []
        async for raw in server._stream(rt, "hi", "m", "chat-1"):
            line = raw.decode().removeprefix("data: ").strip()
            out.append(line if line == "[DONE]" else json.loads(line))
        return out
    return asyncio.run(collect())


def _kind(chunk):
    if chunk == "[DONE]":
        return "done"
    if chunk.get("event", {}).get("type") == "status":
        data = chunk["event"]["data"]
        return "status-hide" if data.get("hidden") else "status-show"
    if chunk.get("usage"):
        return "usage"
    choice = chunk["choices"][0]
    if choice.get("finish_reason"):
        return "finish"
    delta = choice["delta"]
    return "role" if delta.get("role") else "content"


def test_status_shows_at_once_and_hides_before_the_first_words():
    kinds = [_kind(c) for c in _events(_Runtime(["", "Hel", "lo"]))]
    assert kinds[:4] == ["role", "status-show", "status-hide", "content"]
    assert kinds.count("status-hide") == 1 and kinds[-3:] == ["finish", "usage", "done"]


def test_status_is_metadata_not_message_content():
    chunks = _events(_Runtime(["Hello"]))
    text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks
                   if c != "[DONE]" and c.get("choices"))
    assert text == "Hello"


def test_a_reply_with_no_words_still_hides_the_status():
    kinds = [_kind(c) for c in _events(_Runtime([]))]
    assert kinds == ["role", "status-show", "status-hide", "finish", "usage", "done"]


def test_a_failed_turn_hides_the_status_and_still_raises():
    seen = []

    async def run():
        async for raw in server._stream(_Runtime(error=RuntimeError("boom")), "hi", "m", "c"):
            seen.append(_kind(json.loads(raw.decode().removeprefix("data: "))))

    with pytest.raises(RuntimeError):
        asyncio.run(run())
    assert seen == ["role", "status-show", "status-hide"]


def _status_lines(chunks):
    return [(c["event"]["data"]["description"], bool(c["event"]["data"].get("hidden")))
            for c in chunks if c != "[DONE]" and c.get("event", {}).get("type") == "status"]


def test_a_running_tool_shows_its_own_status_line_then_clears_it():
    from hubzoid.tool_events import Status

    chunks = _events(_Runtime([Status("Running check_program…"), "<details>…</details>",
                               Status(None), "The answer"]))
    assert _status_lines(chunks) == [
        ("Working on it…", False), ("Running check_program…", False),
        ("Working on it…", True),  # cleared (the chat app hides the line)
    ]
    text = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks
                   if c != "[DONE]" and c.get("choices"))
    assert text == "<details>…</details>The answer"  # a status is never message text


def test_a_tool_line_left_showing_is_hidden_at_the_end_and_on_failure():
    from hubzoid.tool_events import Status

    lines = _status_lines(_events(_Runtime([Status("Running x…"), "partial"])))
    assert lines[-1][1] is True and lines[-2] == ("Running x…", False)
    seen = []

    async def run():
        async for raw in server._stream(_Runtime([Status("Running x…")], error=RuntimeError("boom")),
                                        "hi", "m", "c"):
            seen.append(json.loads(raw.decode().removeprefix("data: ")))

    with pytest.raises(RuntimeError):
        asyncio.run(run())
    assert _status_lines(seen)[-1][1] is True
