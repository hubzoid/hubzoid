"""1.0.x assistant markup to web app message parts (contract 6.4), with the
exact text ``tool_events`` and ``factory_claude._ThinkStream`` write."""
from __future__ import annotations

from hubzoid import tool_events
from hubzoid.factory_claude import _ThinkStream
from hubzoid.migrate_openwebui import assistant_parts, parts_text, preview_args


def _parts(text: str, mid: str = "m1") -> list[dict]:
    return assistant_parts({"content": text}, mid)


def test_compact_and_full_tool_markup_as_emitted_today():
    text = (
        "Let me look."
        + tool_events.format_call("read_knowledge", {"name": "revenue", "limit": 5}, mode="compact")
        + tool_events.format_call("list_files", None, mode="compact")
        + tool_events.format_call("search", {"query": "q3 numbers"}, mode="full")
        + "Here it is."
    )
    parts = _parts(text)
    assert [p["type"] for p in parts] == ["text", "tool-call", "tool-call", "tool-call", "text"]
    assert parts[0] == {"type": "text", "text": "Let me look."}
    assert parts[1] == {"type": "tool-call", "toolCallId": "m1-t1", "toolName": "read_knowledge",
                        "args": {"name": "revenue", "limit": "5"}, "result": {"status": "ok"}}
    assert parts[2]["toolName"] == "list_files" and parts[2]["args"] == {}
    assert parts[3]["toolName"] == "search" and parts[3]["args"] == {"query": "q3 numbers"}
    assert parts[4] == {"type": "text", "text": "Here it is."}


def test_older_check_mark_markup_and_json_previews():
    text = ('\n\n<details>\n<summary>✓ mcp__gh_repo__list_issues</summary>\n\n`{"repo": "a/b"}`\n\n</details>\n\n'
            '\n\n> ✓ **whoami**\n\nDone.')
    parts = _parts(text)
    assert parts[0]["toolName"] == "mcp__gh_repo__list_issues" and parts[0]["args"] == {"repo": "a/b"}
    assert parts[1]["toolName"] == "whoami" and parts[1]["args"] == {}
    assert parts[2]["text"] == "Done."


def test_error_marks_the_preceding_call_of_that_name():
    text = (tool_events.format_call("ledger_lookup", {"account": "4000"}, mode="full")
            + tool_events.format_call("other", None, mode="compact")
            + tool_events.format_error("ledger_lookup", "Permission denied\nsecond line")
            + "I could not read it.")
    parts = _parts(text)
    calls = [p for p in parts if p["type"] == "tool-call"]
    assert calls[0]["result"] == {"status": "error", "message": "Permission denied"}
    assert calls[1]["result"] == {"status": "ok"}
    assert parts[-1] == {"type": "text", "text": "I could not read it."}


def test_error_without_a_call_is_a_standalone_failed_call():
    parts = _parts(tool_events.format_error("mcp__hubzoid__read_upload", "missing file") + "Sorry.")
    assert parts[0]["type"] == "tool-call"
    assert parts[0]["toolName"] == "mcp__hubzoid__read_upload"
    assert parts[0]["result"] == {"status": "error", "message": "missing file"}
    assert parts[0]["args"] == {}


def test_error_matches_a_short_name():
    text = (tool_events.format_call("mcp__hubzoid__read_knowledge", {"name": "x"}, mode="full")
            + tool_events.format_error("read_knowledge", "nope"))
    parts = _parts(text)
    assert len(parts) == 1 and parts[0]["result"]["status"] == "error"


def test_think_blocks_indicator_and_full():
    indicator = _ThinkStream("indicator")
    text = indicator.thinking("secret") + indicator.visible("Answer one.")
    text += indicator.thinking("more") + indicator.close()
    parts = _parts(text)
    assert parts == [{"type": "reasoning", "text": ""}, {"type": "text", "text": "Answer one."},
                     {"type": "reasoning", "text": ""}]
    full = _ThinkStream("full")
    parts = _parts(full.thinking("Step one. ") + full.thinking("Step two.") + full.visible("Done."))
    assert parts == [{"type": "reasoning", "text": "Step one. Step two."}, {"type": "text", "text": "Done."}]


def test_unclosed_think_block_is_reasoning_to_the_end():
    assert _parts("<think>\ncut off mid thought") == [{"type": "reasoning", "text": "cut off mid thought"}]


def test_markup_inside_fenced_code_stays_text():
    text = "Example:\n\n```md\n> ↳ **not_a_tool** `x=1`\n<think>no</think>\n```\n\nEnd."
    assert _parts(text) == [{"type": "text", "text": text}]


def test_open_webui_reasoning_and_tool_details():
    text = ('<details type="reasoning" done="true" duration="2">\n<summary>Thought for 2 seconds</summary>\n'
            '> step one\n> step two\n</details>\n'
            '<details type="tool_calls" done="true" id="call_1" name="web_search" '
            'arguments="{&quot;q&quot;: &quot;x&quot;}" result="&quot;ok&quot;">\n<summary>Tool Executed</summary>\n'
            '</details>\nFinal.')
    parts = _parts(text)
    assert parts[0] == {"type": "reasoning", "text": "step one\nstep two"}
    assert parts[1] == {"type": "tool-call", "toolCallId": "call_1", "toolName": "web_search",
                        "args": {"q": "x"}, "result": {"status": "ok"}}
    assert parts[2] == {"type": "text", "text": "Final."}


def test_open_webui_output_items():
    message = {"content": "", "output": [
        {"type": "reasoning", "content": [{"type": "output_text", "text": "_Thinking…_"}], "summary": None},
        {"type": "message", "content": [{"type": "output_text", "text":
            tool_events.format_call("whoami", None, mode="compact") + "Hello."}]},
        {"type": "function_call", "call_id": "c9", "name": "calendar_list", "arguments": '{"day": "mon"}'},
        {"type": "function_call_output", "call_id": "c9", "status": "failed", "error": "down"},
        {"type": "reasoning", "content": [], "summary": [{"type": "summary_text", "text": "Summary."}]},
        {"type": "message", "content": [{"type": "output_text", "text": tool_events.format_error("whoami", "late")}]},
    ]}
    parts = assistant_parts(message, "mx")
    assert [p["type"] for p in parts] == ["reasoning", "tool-call", "text", "tool-call", "reasoning"]
    assert parts[0]["text"] == ""
    assert parts[1]["toolName"] == "whoami" and parts[1]["result"] == {"status": "error", "message": "late"}
    assert parts[3]["toolCallId"] == "c9" and parts[3]["args"] == {"day": "mon"}
    assert parts[3]["result"] == {"status": "error", "message": "down"}
    assert parts[4]["text"] == "Summary."


def test_output_wins_over_content_and_empty_messages():
    message = {"content": "Old text", "output": [{"type": "message", "content": [{"type": "output_text", "text": "New"}]}]}
    assert assistant_parts(message, "m") == [{"type": "text", "text": "New"}]
    assert assistant_parts({"content": ""}, "m") == []
    assert assistant_parts({"content": None, "output": []}, "m") == []


def test_preview_args():
    assert preview_args("") == {}
    assert preview_args("name=jexl") == {"name": "jexl"}
    assert preview_args("query=two words limit=5") == {"query": "two words", "limit": "5"}
    assert preview_args('{"a": 1}') == {"a": 1}
    assert preview_args('{"a": "cut…') == {"preview": '{"a": "cut…'}
    assert preview_args("a=1 b=2 c=3") == {"preview": "a=1 b=2 c=3"}
    assert preview_args("free text") == {"preview": "free text"}


def test_text_around_an_attached_error_is_one_part():
    parts = _parts("\n\nFirst.\n\n" + tool_events.format_call("x", None, mode="full") + "Middle.\n\n"
                   + tool_events.format_error("x", "y") + "\n\nSecond.\n")
    assert [p["type"] for p in parts] == ["text", "tool-call", "text"]
    assert parts[1]["result"] == {"status": "error", "message": "y"}
    assert parts[2] == {"type": "text", "text": "Middle.\n\nSecond."}
    assert parts_text(parts + [{"type": "file", "name": "a.pdf"}]) == "First.\n\nMiddle.\n\nSecond.\n\na.pdf"
