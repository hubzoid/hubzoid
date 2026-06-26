"""Tests for the tool-activity blockquote formatter.

One line per tool call: ``> ✓ **name** `args```. Errors get a separate
``> ⚠ **name** message`` line because the agent's reply may not always
surface failures clearly. No matching "returned" line on success.
"""
from __future__ import annotations

from hubzoid import tool_events


# ---------------------------------------------------------------------------
# format_call: one line per call, ✓ icon, no result size
# ---------------------------------------------------------------------------
def test_format_call_with_dict_args():
    out = tool_events.format_call("read_knowledge", {"name": "jexl-expressions"})
    assert "✓" in out
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
    assert out.startswith("\n\n> ✓ ")
    assert "**read_knowledge**" in out


def test_format_call_default_mode_is_full_blockquote():
    """Back-compat: callers that don't pass a mode get the legacy blockquote."""
    out = tool_events.format_call("list_skills")
    assert out.startswith("\n\n> ✓ ")


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
# Regression: ensure format_result no longer exists as a public symbol
# (was removed in favor of the one-line UX).
# ---------------------------------------------------------------------------
def test_format_result_removed():
    assert not hasattr(tool_events, "format_result")
