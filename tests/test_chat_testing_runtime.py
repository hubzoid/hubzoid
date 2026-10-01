"""The scripted test runtime (MODEL=hubzoid-test/..., HUBZOID_TEST_RUNTIME=1).

Deterministic replies chosen by keywords, typed events like a real backend,
refused unless explicitly enabled, and a scripted answer for complete_once.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import create_engine, text

from hubzoid import _request_ctx, runtime, testing_runtime
from hubzoid.run_events import Notice, ReasoningDelta, ReasoningEnd, ToolCall, ToolResult, text_of


@pytest.fixture
def hub(tmp_path, monkeypatch):
    hub = tmp_path / "scripted-hub"
    hub.mkdir()
    (hub / "AGENTS.md").write_text("---\nname: scripted\ndescription: d\n---\nHelp.\n")
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    monkeypatch.setenv("MODEL", "hubzoid-test/scripted")
    monkeypatch.setenv("HUBZOID_TEST_RUNTIME", "1")
    monkeypatch.setenv("HUBZOID_TEST_SLOW_SECONDS", "0.2")
    monkeypatch.setenv("BRIDGE_PORT", "3399")
    monkeypatch.delenv("HUBZOID_PUBLIC_URL", raising=False)
    monkeypatch.delenv("WEBUI_URL", raising=False)
    for key in ("SHOW_TOOLS", "SHOW_THINKING"):
        monkeypatch.delenv(key, raising=False)
    return hub


def _items(rt, prompt, chat="chat-scripted-1"):
    async def go():
        with _request_ctx.chat_scope(chat):
            items = [i async for i in rt.stream_events(prompt)]
            return items, _request_ctx.drain_usage()

    return asyncio.run(go())


def test_build_selects_the_scripted_runtime(hub):
    rt = runtime.build(hub)
    assert isinstance(rt, testing_runtime.ScriptedRuntime)
    assert rt.name == "scripted"
    assert json.loads(runtime.describe(hub))["backend"] == "hubzoid-test"


def test_refused_without_the_flag(hub, monkeypatch):
    monkeypatch.delenv("HUBZOID_TEST_RUNTIME")
    with pytest.raises(RuntimeError, match="HUBZOID_TEST_RUNTIME=1"):
        runtime.build(hub)
    monkeypatch.setenv("HUBZOID_TEST_RUNTIME", "true")  # only "1" enables it
    with pytest.raises(RuntimeError, match="HUBZOID_TEST_RUNTIME=1"):
        runtime.build(hub)
    with pytest.raises(RuntimeError, match="HUBZOID_TEST_RUNTIME=1"):
        runtime.complete_once(hub, {"prompt": "x"})


def test_default_reply_quotes_the_latest_request(hub):
    rt = runtime.build(hub)
    items, usage = _items(rt, "[user]\nfirst question\n\n[assistant]\nold\n\n[user]\nWhat is the plan?")
    assert all(isinstance(i, str) for i in items)
    reply = "".join(items)
    assert reply.startswith("You said: “What is the plan?”")
    assert "**scripted**" in reply
    assert usage["model"] == "hubzoid-test/scripted" and usage["status"] == "ok"
    assert usage["cost_usd"] == 0.0 and usage["output_tokens"] > 0


def test_attachments_are_listed(hub):
    rt = runtime.build(hub)
    prompt = ("[user]\n[Image: cat.png]  (attached image, shown to you directly)\n\n"
              "[User attached file: notes.txt (5 bytes, text/plain). Read it with "
              "read_upload('notes.txt'), or pass its on-disk path to a path-accepting "
              "tool or script: /x/notes.txt]\n\nWhat is this?")
    reply = "".join(_items(rt, prompt)[0])
    assert "You said: “What is this?”" in reply
    assert "Attached: `cat.png`, `notes.txt`." in reply


def test_tool_think_and_fail_scripts(hub, monkeypatch):
    monkeypatch.setenv("SHOW_THINKING", "full")
    rt = runtime.build(hub)
    items, _ = _items(rt, "[user]\nthink, use a tool, then fail")
    kinds = [type(i) for i in items if not isinstance(i, str)]
    assert kinds == [ReasoningDelta, ReasoningDelta, ReasoningDelta, ReasoningEnd,
                     ToolCall, ToolResult, ToolCall, ToolResult]
    calls = [i for i in items if isinstance(i, ToolCall)]
    results = [i for i in items if isinstance(i, ToolResult)]
    assert [c.name for c in calls] == ["read_knowledge", "grep_data"]
    assert [r.ok for r in results] == [True, False]
    assert "".join(i.text for i in items if isinstance(i, ReasoningDelta)) == \
        "Considering the request step by step."


def test_indicator_and_off_thinking(hub, monkeypatch):
    rt = runtime.build(hub)  # SHOW_THINKING default: indicator
    items, _ = _items(rt, "[user]\nthink")
    deltas = [i for i in items if isinstance(i, ReasoningDelta)]
    assert deltas == [ReasoningDelta(text="", legacy="<think>\n_Thinking…_")]
    monkeypatch.setenv("SHOW_THINKING", "off")
    rt = runtime.build(hub)
    items, _ = _items(rt, "[user]\nthink")
    assert not [i for i in items if isinstance(i, (ReasoningDelta, ReasoningEnd))]


def test_error_script_fails_the_run(hub):
    rt = runtime.build(hub)
    items, usage = _items(rt, "[user]\nplease error")
    assert isinstance(items[-1], Notice) and items[-1].kind == "error"
    assert rt.last_error is not None and usage["status"] == "error"
    assert "[agent error: RuntimeError: scripted failure]" in "".join(text_of(i) for i in items)


def test_artifact_script_writes_a_file_and_the_footer(hub):
    from hubzoid import memory

    rt = runtime.build(hub)
    items, _ = _items(rt, "[user]\nmake an artifact", chat="chat-artifact-1")
    target = memory.chat_artifact_dir(hub, "chat-artifact-1") / testing_runtime.ARTIFACT_NAME
    assert target.is_file() and "Scripted report" in target.read_text()
    footer = items[-1]
    assert isinstance(footer, Notice) and footer.kind == "artifacts"
    assert "/artifacts/chat-artifact-1/scripted-report.md?t=" in footer.text
    call = next(i for i in items if isinstance(i, ToolCall))
    assert call.name == "write_artifact"


def test_markdown_script(hub):
    rt = runtime.build(hub)
    reply = "".join(_items(rt, "[user]\nshow markdown")[0])
    assert "| Item | Count |" in reply and "```python" in reply


def test_slow_script_streams_forty_chunks(hub):
    rt = runtime.build(hub)
    items, _ = _items(rt, "[user]\nbe slow")
    assert items[0] == "Chunk 1. " and items[-1] == "Chunk 40. " and len(items) == 40


def test_slow_script_can_be_cancelled(hub, monkeypatch):
    monkeypatch.setenv("HUBZOID_TEST_SLOW_SECONDS", "20")
    rt = runtime.build(hub)

    async def go():
        seen = []

        async def consume():
            async for item in rt.stream_events("[user]\nslow"):
                seen.append(item)

        task = asyncio.create_task(consume())
        while len(seen) < 2:
            await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return seen

    seen = asyncio.run(go())
    assert 2 <= len(seen) < 40


def test_legacy_text_uses_the_configured_modes(hub, monkeypatch):
    monkeypatch.setenv("SHOW_TOOLS", "full")
    rt = runtime.build(hub)
    text_out = asyncio.run(rt.run("[user]\ntool and fail"))
    assert "> ↳ **read_knowledge** `name=scripted`" in text_out
    assert "> ⚠ **grep_data**" in text_out


def test_complete_once_answers_with_a_title_and_labels_usage(hub, tmp_path):
    out = runtime.complete_once(hub, {"prompt": "Write a title.\n\nMessage:\nPlan the quarterly budget review"},
                                subject="ana@example.org", surface="web", kind="background",
                                chat_id="c_title_1")
    assert out["text"] == "About plan the quarterly budget"
    assert out["model"] == "hubzoid-test/scripted"
    json_out = runtime.complete_once(hub, {"prompt": "x", "response_format": "json"})
    assert json_out["json"] == {"answer": "scripted"}
    with create_engine(f"sqlite:///{tmp_path / 'ops.db'}").connect() as c:
        rows = c.execute(text("SELECT surface, kind, subject, chat_id, status FROM hz_usage "
                              "ORDER BY ts")).fetchall()
    assert tuple(rows[0]) == ("web", "background", "ana@example.org", "c_title_1", "ok")
    assert tuple(rows[1])[:2] == ("workflow", "llm")
