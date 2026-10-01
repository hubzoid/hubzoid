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
def test_indicator_opens_an_empty_panel_then_closes_on_answer():
    """The chat app shows its own "Thinking…" line and timer for an open block;
    no placeholder text (it rendered as a quoted "Thinking…" inside the panel)."""
    tw = _ThinkStream("indicator")
    first = tw.thinking("secret reasoning the user must not see")
    assert first == "<think>\n"
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


def test_later_bursts_are_a_status_line_in_indicator_mode():
    """Only the first burst (before anything is shown) is a panel; later ones
    show the THINKING status line, once per burst, so tool rows are not split
    by empty "Thought for less than a second" panels."""
    from hubzoid.tool_events import THINKING, Status

    tw = _ThinkStream("indicator")
    tw.thinking("burst 1")
    assert tw.visible("> tool call").startswith("\n</think>\n")  # closes burst 1
    later = tw.thinking("burst 2")
    assert isinstance(later, Status) and later.description == THINKING
    assert tw.thinking("more of burst 2") == ""  # once per burst
    assert tw.visible("answer") == "answer"  # nothing to close
    assert isinstance(tw.thinking("burst 3"), Status)


def test_full_mode_opens_a_panel_per_burst():
    tw = _ThinkStream("full")
    tw.thinking("burst 1")
    tw.visible("> tool call")
    assert tw.thinking("burst 2") == "<think>\nburst 2"


def test_close_is_idempotent_and_safe_when_never_opened():
    tw = _ThinkStream("indicator")
    assert tw.close() == ""  # nothing open
    tw.thinking("x")
    assert tw.close() == "\n</think>\n"
    assert tw.close() == ""  # already closed


def _claude_turn(monkeypatch, messages, *, tool_mode="compact", thinking_mode="indicator"):
    """Run ClaudeRuntime.stream over fake SDK messages; returns the parts."""
    import asyncio

    import claude_agent_sdk

    from hubzoid.factory_claude import ClaudeRuntime

    def fake_query(*, prompt, options):
        async def gen():
            for m in messages:
                yield m
        return gen()

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    rt = ClaudeRuntime(name="x", options=object(), tool_mode=tool_mode, thinking_mode=thinking_mode)

    async def collect():
        return [p async for p in rt.stream("hi")]

    return asyncio.run(collect())


def _delta(kind, text):
    from claude_agent_sdk.types import StreamEvent

    key = "thinking" if kind == "thinking_delta" else "text"
    return StreamEvent(uuid="u", session_id="s", event={
        "type": "content_block_delta", "delta": {"type": kind, key: text}})


def test_claude_tools_render_as_one_foldable_run(monkeypatch):
    """Thinking, two parallel calls (one fails), more thinking, then the answer:
    status lines while the calls run, ✓/✗ tool blocks as they finish, empty
    thinking panels, and no quoted ⚠ or "Thinking…" text."""
    from claude_agent_sdk import AssistantMessage, UserMessage
    from claude_agent_sdk.types import ToolResultBlock, ToolUseBlock

    from hubzoid.tool_events import Status

    parts = _claude_turn(monkeypatch, [
        _delta("thinking_delta", "plan the lookups"),
        AssistantMessage(content=[ToolUseBlock(id="t1", name="mcp__hubzoid__check_program",
                                               input={"event_id": 1556}),
                                  ToolUseBlock(id="t2", name="finance_review_report", input={})],
                         model="claude-x"),
        UserMessage(content=[ToolResultBlock(tool_use_id="t1", content="ok")]),
        UserMessage(content=[ToolResultBlock(tool_use_id="t2", content="boom", is_error=True)]),
        _delta("thinking_delta", "now answer"),
        _delta("text_delta", "Event 1556 is approved."),
    ])
    statuses = [p.description for p in parts if isinstance(p, Status)]
    assert statuses == ["Running check_program…", "Running check_program and finance_review_report…",
                        "Running finance_review_report…", None, "Thinking…"]
    text = "".join(parts)
    assert text.count('<details type="tool_calls"') == 2
    assert 'name="check_program"' in text and "mcp__hubzoid__" not in text
    assert 'id="t2" name="finance_review_report" arguments="{}" status="failed"' in text
    # One empty panel for the first burst, closed before the tools run (no
    # spinner while they run); the second burst is a status line.
    assert text.count("<think>") == 1 and "<think>\n\n</think>" in text
    assert text.index("</think>") < text.index("<details")
    # The two blocks touch (one newline), so the chat app folds them together.
    assert '</details>\n<details type="tool_calls"' in text
    assert "⚠" not in text and "Thinking…" not in text
    assert text.rstrip().endswith("Event 1556 is approved.")


def test_claude_a_turn_cut_short_still_writes_its_calls(monkeypatch):
    from claude_agent_sdk import AssistantMessage
    from claude_agent_sdk.types import ToolUseBlock

    from hubzoid.tool_events import Status

    parts = _claude_turn(monkeypatch, [
        AssistantMessage(content=[ToolUseBlock(id="t1", name="slow", input={})], model="m"),
    ])
    assert "".join(parts).count('name="slow"') == 1
    assert [p.description for p in parts if isinstance(p, Status)] == ["Running slow…", None]
