"""Unit tests for the SHOW_THINKING surfacing logic (no network)."""
from __future__ import annotations

from hubzoid import reasoning
from hubzoid.factory_claude import _ThinkStream


# --- mode normalization ----------------------------------------------------
def test_normalize_thinking_default_is_indicator():
    assert reasoning.normalize_thinking(None) == "indicator"
    assert reasoning.normalize_thinking("") == "indicator"
    assert reasoning.normalize_thinking("garbage") == "indicator"


def test_normalize_thinking_aliases():
    assert reasoning.normalize_thinking("full") == "full"
    assert reasoning.normalize_thinking("true") == "full"
    assert reasoning.normalize_thinking("text") == "full"
    assert reasoning.normalize_thinking("off") == "off"
    assert reasoning.normalize_thinking("false") == "off"
    assert reasoning.normalize_thinking("INDICATOR") == "indicator"


# --- thinking config -------------------------------------------------------
def test_config_off_returns_none():
    assert reasoning.claude_thinking_config(None, "off") is None
    assert reasoning.claude_thinking_config("high", "off") is None


def test_config_indicator_adaptive_when_no_effort():
    cfg = reasoning.claude_thinking_config(None, "indicator")
    assert cfg == {"type": "adaptive", "display": "summarized"}


def test_config_full_uses_budget_when_effort_set():
    cfg = reasoning.claude_thinking_config("medium", "full")
    assert cfg == {"type": "enabled", "budget_tokens": 12_000, "display": "summarized"}


# --- <think> wrapping state machine ---------------------------------------
def test_indicator_emits_placeholder_then_closes_on_answer():
    tw = _ThinkStream("indicator")
    first = tw.thinking("secret reasoning the user must not see")
    assert first.startswith("<think>")
    assert "Thinking" in first
    assert "secret reasoning" not in first  # indicator hides the real text
    # second thinking delta in the same burst adds nothing new
    assert tw.thinking("more secret reasoning") == ""
    out = tw.visible("the answer")
    assert out == "\n</think>\n" + "the answer"


def test_full_streams_real_reasoning_text():
    tw = _ThinkStream("full")
    a = tw.thinking("step one")
    b = tw.thinking(" step two")
    assert a == "<think>\nstep one"
    assert b == " step two"
    assert tw.visible("X") == "\n</think>\nX"


def test_new_block_opens_after_a_tool_line():
    tw = _ThinkStream("indicator")
    tw.thinking("burst 1")
    assert tw.visible("> tool call").startswith("\n</think>\n")  # closes burst 1
    reopened = tw.thinking("burst 2")
    assert reopened.startswith("<think>")  # a fresh panel for the next burst


def test_close_is_idempotent_and_safe_when_never_opened():
    tw = _ThinkStream("indicator")
    assert tw.close() == ""  # nothing open
    tw.thinking("x")
    assert tw.close() == "\n</think>\n"
    assert tw.close() == ""  # already closed
