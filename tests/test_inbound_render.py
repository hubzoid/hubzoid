"""Surface-agnostic render steps shared by every chat surface: strip the
reasoning panel, strip tool-call dropdowns, cap the length. Formatting flavor
(WhatsApp vs Telegram) lives in each surface's own conversion module."""
from hubzoid.inbound.render import strip_thinking, strip_tool_calls, truncate


def test_strip_thinking_removes_closed_block():
    visible, active = strip_thinking("a<think>reason</think>b")
    assert visible == "ab"
    assert active is False


def test_strip_thinking_flags_unclosed_and_cuts_the_tail():
    visible, active = strip_thinking("answer <think>still going")
    assert visible == "answer "
    assert active is True


def test_strip_thinking_noop_when_absent():
    visible, active = strip_thinking("plain answer")
    assert visible == "plain answer"
    assert active is False


def test_strip_tool_calls_removes_details_block():
    assert strip_tool_calls("a<details>tool ran</details>b") == "ab"


def test_strip_tool_calls_drops_unclosed_details_tail():
    assert strip_tool_calls("answer<details>streaming…") == "answer"


def test_strip_tool_calls_noop_when_absent():
    assert strip_tool_calls("plain answer") == "plain answer"


def test_truncate_under_limit_is_unchanged():
    assert truncate("hello", limit=10) == "hello"


def test_truncate_over_limit_appends_marker_within_budget():
    out = truncate("abcdefghij", limit=5, marker="…")
    assert out.endswith("…")
    assert len(out) <= 5
