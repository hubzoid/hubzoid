"""Slack <think> stripping + Thinking… indicator (no network)."""
from __future__ import annotations

from hubzoid.slack.adapter import _THINKING_STATUS, _slack_render
from hubzoid.slack.conversion import strip_thinking


# --- strip_thinking --------------------------------------------------------
def test_no_think_is_passthrough():
    assert strip_thinking("just an answer") == ("just an answer", False)


def test_closed_block_removed():
    visible, active = strip_thinking("<think>\n_Thinking…_\n</think>\nThe answer")
    assert "<think>" not in visible
    assert "Thinking" not in visible
    assert visible.strip() == "The answer"
    assert active is False


def test_unclosed_block_flags_active_and_is_stripped():
    visible, active = strip_thinking("partial answer <think>\nreasoning so far")
    assert visible == "partial answer "
    assert active is True


def test_multiple_blocks_and_tool_lines_preserved():
    raw = (
        "<think>\n_Thinking…_\n</think>\n"
        "> ✓ read_knowledge `name=payment-config`\n"
        "<think>\n_Thinking…_\n</think>\n"
        "Namaskaram\nHere's the fix."
    )
    visible, active = strip_thinking(raw)
    assert active is False
    assert "<think>" not in visible and "_Thinking…_" not in visible
    assert "read_knowledge" in visible  # tool activity survives
    assert "Namaskaram" in visible


def test_full_mode_reasoning_text_is_stripped_too():
    raw = "<think>\nThe user's seat was released because GPMS...\n</think>\nAnswer"
    visible, _ = strip_thinking(raw)
    assert "GPMS" not in visible
    assert visible.strip() == "Answer"


# --- _slack_render ---------------------------------------------------------
def test_render_thinking_only_shows_indicator():
    assert _slack_render("<think>\n_Thinking…_") == _THINKING_STATUS


def test_render_answer_only_no_indicator_no_reasoning():
    out = _slack_render("<think>\nsecret\n</think>\nThe final answer")
    assert out == "The final answer"
    assert "Thinking" not in out


def test_render_appends_indicator_while_reasoning_after_tool_calls():
    # second reasoning burst (unclosed) after a tool line -> show both
    raw = (
        "<think>\n_Thinking…_\n</think>\n"
        "> ✓ read_knowledge `name=payment-config`\n"
        "<think>\nstill reasoning"
    )
    out = _slack_render(raw)
    assert "read_knowledge" in out          # prior visible content kept
    assert _THINKING_STATUS in out          # live indicator appended
    assert "still reasoning" not in out     # reasoning text never leaks


def test_render_empty_when_nothing_yet():
    assert _slack_render("") == ""
