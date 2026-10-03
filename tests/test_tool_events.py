"""Tests for the tool-activity blockquote formatter.

One line per tool call: ``> ↳ **name** `args```. Errors get a separate
``> ⚠ **name** message`` line because the agent's reply may not always
surface failures clearly. No matching "returned" line on success.
"""
from __future__ import annotations

from hubzoid import tool_events


# ---------------------------------------------------------------------------
# format_call: one line per call, ↳ icon, no result size
# ---------------------------------------------------------------------------
def test_format_call_with_dict_args():
    out = tool_events.format_call("read_knowledge", {"name": "jexl-expressions"})
    assert "↳" in out
    assert "**read_knowledge**" in out
    assert "name=jexl-expressions" in out
    # Wrapped in blank-line-padded blockquote for clean rendering.
    assert out.startswith("\n\n> ")
    assert out.endswith("\n\n")


def test_format_call_no_returned_or_size_label():
    """The call line is the ONLY line for a successful call. No size,
    no 'returned', no separate confirmation row.
    """
    out = tool_events.format_call("write_artifact", {"filename": "r.txt"})
    assert "returned" not in out
    assert " B" not in out and "KB" not in out and "MB" not in out


def test_format_call_with_none_args_omits_preview():
    out = tool_events.format_call("list_skills", None)
    assert "**list_skills**" in out


def test_format_call_long_arg_is_truncated():
    long = "x" * 200
    out = tool_events.format_call("write_artifact", {"content": long})
    assert "x" * 200 not in out


def test_format_call_strips_backticks():
    out = tool_events.format_call("eval", {"expr": "`rm -rf /`"})
    body_start = out.index("**eval**")
    body = out[body_start:]
    assert "rm -rf /" in body.replace("`", "")


# ---------------------------------------------------------------------------
# SHOW_TOOLS modes: compact (collapsible dropdown) is the product default,
# full is the legacy inline blockquote, off emits nothing.
# ---------------------------------------------------------------------------
def test_format_call_full_mode_is_inline_blockquote():
    out = tool_events.format_call("read_knowledge", {"name": "jexl"}, mode="full")
    assert out.startswith("\n\n> ↳ ")
    assert "**read_knowledge**" in out


def test_format_call_compact_mode_is_collapsible_details():
    out = tool_events.format_call("read_knowledge", {"name": "jexl"}, mode="compact")
    assert "<details>" in out and "</details>" in out
    assert "<summary>" in out and "</summary>" in out
    assert "read_knowledge" in out
    # A fold, not a raw blockquote line.
    assert not out.lstrip().startswith(">")


def test_format_call_compact_keeps_args_in_body_not_summary():
    """Short label visible (tool name); args revealed only on expand."""
    out = tool_events.format_call("read_knowledge", {"name": "jexl"}, mode="compact")
    summary = out[out.index("<summary>") : out.index("</summary>")]
    assert "name=jexl" not in summary
    assert "name=jexl" in out


def test_format_call_off_mode_emits_nothing():
    assert tool_events.format_call("read_knowledge", {"name": "jexl"}, mode="off") == ""


# ---------------------------------------------------------------------------
# format_error: still emitted for failed calls so the user sees the failure
# ---------------------------------------------------------------------------
def test_format_error_with_message():
    out = tool_events.format_error("read_knowledge", "no such doc 'nope'")
    assert "⚠" in out
    assert "no such doc" in out


def test_format_error_truncates_to_one_line():
    multi = "first line\nsecond line\nthird line"
    out = tool_events.format_error("tool", multi)
    assert "second line" not in out
    assert "first line" in out


# ---------------------------------------------------------------------------
# short_name: strip the mcp__hubzoid__ noise so the user sees clean names
# ---------------------------------------------------------------------------
def test_short_name_strips_mcp_hubzoid_prefix():
    assert tool_events.short_name("mcp__hubzoid__read_knowledge") == "read_knowledge"
    assert tool_events.short_name("read_file") == "read_file"
    assert tool_events.short_name("mcp__other__tool") == "mcp__other__tool"




# ---------------------------------------------------------------------------
# ToolActivity: the chat app's native tool-call blocks and status line.
# ---------------------------------------------------------------------------
import html as _html
import json as _json
import re as _re


def _attrs(block: str) -> dict:
    head = block[block.index("<details"):block.index(">", block.index("<details"))]
    return {k: _html.unescape(v) for k, v in _re.findall(r'(\w+)="([^"]*)"', head)}


def test_compact_shows_a_status_while_running_and_a_block_when_finished():
    act = tool_events.ToolActivity("compact")
    started = act.started("toolu_1", "check_program", {"event_id": 1556, "listing_url": ""})
    assert len(started) == 1 and isinstance(started[0], tool_events.Status)
    assert started[0] == "" and started[0].description == "Running check_program…"
    block, cleared = act.finished("toolu_1")
    attrs = _attrs(block)
    # Exactly the shape Open WebUI parses: type first, done, id, name, JSON args.
    assert block.lstrip().startswith('<details type="tool_calls" done="true"')
    assert attrs["id"] == "toolu_1" and attrs["name"] == "check_program"
    assert _json.loads(attrs["arguments"]) == {"event_id": 1556, "listing_url": ""}
    assert "status" not in attrs and "<summary>Tool Executed</summary>" in block
    assert block.rstrip().endswith("</details>")
    assert isinstance(cleared, tool_events.Status) and cleared.description is None


def test_a_failed_call_is_marked_failed_with_short_text_only():
    act = tool_events.ToolActivity("compact")
    act.started("c1", "finance_review_report", {"id": 3})
    block, _ = act.finished("c1", error=True)
    assert _attrs(block)["status"] == "failed"
    assert tool_events.FAILED_TEXT in block
    assert "> ⚠" not in block  # no separate quoted error line any more


def test_parallel_calls_keep_the_status_on_what_is_still_running():
    act = tool_events.ToolActivity("compact")
    act.started("a", "check_program", {})
    act.started("b", "finance_review_report", {})
    act.started("c", "draft_sl_from_event", {})
    assert act._status().description == "Running check_program, finance_review_report and 1 more…"
    _, status = act.finished("a")
    assert status.description == "Running finance_review_report and draft_sl_from_event…"
    _, status = act.finished("c")
    assert status.description == "Running finance_review_report…"
    assert act.finished("unknown") == []
    flushed = act.flush()  # the turn ended with b unresolved
    assert "finance_review_report" in flushed[0] and flushed[-1].description is None
    assert act.flush() == []


def test_long_arguments_never_land_whole_in_chat():
    act = tool_events.ToolActivity("compact")
    act.started("c1", "write_artifact", {"filename": "r.html", "content": "x" * 50_000})
    block, _ = act.finished("c1")
    args = _json.loads(_attrs(block)["arguments"])
    assert args["filename"] == "r.html" and len(args["content"]) <= 120
    assert len(block) < 900  # it is sent back with later messages too


def test_attribute_values_are_escaped():
    act = tool_events.ToolActivity("compact")
    act.started("c1", 'evil"><script>', {"q": '"/><img src=x>'})
    block, _ = act.finished("c1")
    head = block[block.index("<details"):block.index(">", block.index("<details")) + 1]
    assert "<script" not in head and "<img" not in head
    assert _attrs(block)["arguments"] == _json.dumps({"q": '"/><img src=x>'})


def test_full_mode_keeps_the_legacy_lines_and_off_shows_nothing():
    full = tool_events.ToolActivity("full")
    assert full.started("c1", "grep_data", {"pattern": "x"})[0].startswith("\n\n> ↳ ")
    assert full.finished("c1") == []
    assert full.finished("c1", error=True)[0].startswith("\n\n> ⚠ **grep_data**")
    off = tool_events.ToolActivity("off")
    assert off.started("c1", "grep_data", {}) == [] and off.finished("c1", error=True) == []
    assert off.flush() == []


def test_status_is_invisible_to_text_consumers():
    status = tool_events.Status("Running x…")
    assert "".join(["a", status, "b"]) == "ab" and not status



def test_failed_output_is_the_same_rule_on_every_runtime():
    assert tool_events.failed_output("An error occurred while running the tool. Please try again.")
    assert tool_events.failed_output("[access denied: 'x' requires the y permission]")
    assert tool_events.failed_output([{"type": "text", "text": "[access denied: no"}])
    assert not tool_events.failed_output("Ledger balanced.")
    assert not tool_events.failed_output(None)



def test_blocks_in_a_run_touch_and_text_starts_a_new_run():
    act = tool_events.ToolActivity("compact")
    act.started("a", "one", {})
    act.started("b", "two", {})
    first = act.finished("a")[0]
    second = act.finished("b")[0]
    assert first.startswith("\n\n<details") and first.endswith("</details>\n")
    assert second.startswith("<details")  # right after the first: one group
    assert act.text("Some text") == "Some text"
    act.started("c", "three", {})
    assert act.finished("c")[0].startswith("\n\n<details")  # a new paragraph after text
    assert act.text("") == ""  # empty text changes nothing
