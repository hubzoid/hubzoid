"""Tool results recorded for evals: `_request_ctx.tool_result_recorder`.

Every runtime records what each tool call returned, keyed by its call id, but
only while the eval runner listens. The typed events and the chat surfaces are
unchanged (tests/test_run_events.py checks every runtime's output against the
recorded 1.0.x text). Also the scripted test runtime's knowledge-aware `tool`
script and its `recall` keyword, which the end-to-end eval tests use.
"""
from __future__ import annotations

import asyncio

from hubzoid import _request_ctx, testing_runtime
from tests import run_event_scenarios as scenarios


def test_record_tool_result_is_a_noop_outside_a_recorder():
    _request_ctx.record_tool_result("c1", "data")     # must not raise
    assert _request_ctx._current_tool_results.get() is None


def test_recorder_collects_and_then_stops():
    with _request_ctx.tool_result_recorder() as results:
        _request_ctx.record_tool_result("c1", "hello")
    _request_ctx.record_tool_result("c2", "after")
    assert results == {"c1": "hello"}


def test_result_text_handles_strings_blocks_and_values():
    text = _request_ctx._result_text
    assert text("x" * 5000) == "x" * _request_ctx.RESULT_KEEP_CHARS
    assert text([{"type": "text", "text": "a"}, {"type": "image"}, {"type": "text", "text": "b"}]) == "a\nb"
    assert text({"rows": 3}) == '{"rows": 3}'
    assert text(None) == ""


def test_openai_results_are_recorded_by_call_id(monkeypatch):
    with _request_ctx.tool_result_recorder() as results:
        scenarios.run_openai(monkeypatch, "tool_then_text", tool_mode="compact", typed=True)
    assert results == {"call_1": "JEXL is an expression language."}


def test_openai_failed_tool_records_the_error_text(monkeypatch):
    with _request_ctx.tool_result_recorder() as results:
        scenarios.run_openai(monkeypatch, "tool_error_output", tool_mode="compact", typed=True)
    assert "Error: boom" in results["call_1"]


def test_claude_results_are_recorded_by_tool_use_id(monkeypatch):
    with _request_ctx.tool_result_recorder() as results:
        scenarios.run_claude(monkeypatch, "tool_error", tool_mode="compact",
                             thinking_mode="off", typed=True)
    assert results == {"t1": "r"}


def test_codex_results_are_recorded_for_offered_tools_only(monkeypatch, tmp_path):
    with _request_ctx.tool_result_recorder() as results:
        scenarios.run_codex(monkeypatch, tmp_path, "tool_ok", tool_mode="compact", typed=True)
    assert results == {"c1": "found x"}
    with _request_ctx.tool_result_recorder() as failed:
        scenarios.run_codex(monkeypatch, tmp_path, "tool_failure", tool_mode="compact", typed=True)
    assert failed == {"c1": "Tool failed. Check the hub server logs."}
    with _request_ctx.tool_result_recorder() as unknown:
        scenarios.run_codex(monkeypatch, tmp_path, "unknown_tool", tool_mode="compact", typed=True)
    assert unknown == {}


# ---------------------------------------------------------------------------
# The scripted test runtime
# ---------------------------------------------------------------------------
def _scripted(tmp_path, monkeypatch):
    hub = tmp_path / "scripted-hub"
    (hub / "knowledge").mkdir(parents=True)
    (hub / "AGENTS.md").write_text("---\nname: scripted\ndescription: d\n---\nHelp.\n")
    (hub / "knowledge" / "refund-policy.md").write_text(
        "---\nname: refund-policy\ndescription: d\n---\nRefunds within 14 days.\n")
    monkeypatch.setenv("HUBZOID_TEST_RUNTIME", "1")
    rt = testing_runtime.ScriptedRuntime(hub, name="scripted", model_id="hubzoid-test/scripted")
    return hub, rt


def _run(rt, prompt):
    async def go():
        with _request_ctx.chat_scope("chat-1"), _request_ctx.tool_result_recorder() as results:
            items = [i async for i in rt.stream_events(prompt)]
            return items, results

    return asyncio.run(go())


def test_scripted_tool_names_the_knowledge_file_in_the_request(tmp_path, monkeypatch):
    _hub, rt = _scripted(tmp_path, monkeypatch)
    items, results = _run(rt, "Use a tool to read the refund-policy please")
    call = next(i for i in items if getattr(i, "name", None) == "read_knowledge")
    assert call.args == {"name": "refund-policy"}
    assert results == {"call_tool": "Refunds within 14 days."}


def test_scripted_tool_without_a_named_file_keeps_its_old_argument(tmp_path, monkeypatch):
    _hub, rt = _scripted(tmp_path, monkeypatch)
    items, results = _run(rt, "use a tool")
    call = next(i for i in items if getattr(i, "name", None) == "read_knowledge")
    assert call.args == {"name": "scripted"}
    assert results == {"call_tool": "Scripted knowledge."}


def test_scripted_fail_records_its_error_text(tmp_path, monkeypatch):
    _hub, rt = _scripted(tmp_path, monkeypatch)
    _items, results = _run(rt, "please fail")
    assert results["call_fail"].startswith("Scripted failure")


def test_scripted_recall_quotes_the_previous_user_message(tmp_path, monkeypatch):
    _hub, rt = _scripted(tmp_path, monkeypatch)
    prompt = "[user]\nMy colour is teal.\n\n[assistant]\nNoted.\n\n[user]\nPlease recall it"
    items, _ = _run(rt, prompt)
    text = "".join(i for i in items if isinstance(i, str))
    assert "Earlier you said: “My colour is teal.”" in text
    items, _ = _run(rt, "[user]\nPlease recall it")
    assert "nothing earlier" in "".join(i for i in items if isinstance(i, str))


def test_previous_request():
    assert testing_runtime.previous_request("[user]\nA\n\n[assistant]\nB\n\n[user]\nC") == "A"
    assert testing_runtime.previous_request("[user]\nonly") is None
    assert testing_runtime.previous_request("plain") is None
