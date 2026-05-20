"""Tests for the pure functions inside the Slack adapter.

These functions transform Slack payloads + bridge SSE lines without doing any
network I/O — easy to cover with plain pytest.
"""
from __future__ import annotations

from hubzoid.slack.conversion import (
    messages_from_thread,
    parse_sse_delta,
    to_slack_mrkdwn,
    truncate_for_slack,
)


# ---------------------------------------------------------------------------
# messages_from_thread
# ---------------------------------------------------------------------------
def test_messages_from_thread_solo_user_message():
    raw = [{"type": "message", "user": "U_USER", "text": "hello", "ts": "1.0"}]
    out = messages_from_thread(raw, bot_user_id="U_BOT")
    assert out == [{"role": "user", "content": "hello"}]


def test_messages_from_thread_marks_bot_as_assistant_by_user_id():
    raw = [
        {"type": "message", "user": "U_USER", "text": "hi", "ts": "1.0"},
        {"type": "message", "user": "U_BOT", "text": "hello back", "ts": "2.0"},
    ]
    out = messages_from_thread(raw, bot_user_id="U_BOT")
    assert out == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello back"},
    ]


def test_messages_from_thread_strips_app_mention_prefix():
    raw = [
        {"type": "message", "user": "U_USER", "text": "<@U_BOT> what is 2+2?", "ts": "1.0"}
    ]
    out = messages_from_thread(raw, bot_user_id="U_BOT")
    assert out == [{"role": "user", "content": "what is 2+2?"}]


def test_messages_from_thread_skips_empty_and_subtype_messages():
    """Slack emits join/leave/channel-rename system messages as subtype='...'.

    These have no semantic value to the agent; filter them out. Likewise empty
    text (e.g. a bot's pending 'Thinking...' placeholder that has already been
    cleared) should not become a turn.
    """
    raw = [
        {"type": "message", "user": "U_USER", "text": "", "ts": "1.0"},
        {"type": "message", "subtype": "channel_join", "user": "U_USER", "ts": "2.0"},
        {"type": "message", "user": "U_USER", "text": "real message", "ts": "3.0"},
    ]
    out = messages_from_thread(raw, bot_user_id="U_BOT")
    assert out == [{"role": "user", "content": "real message"}]


def test_messages_from_thread_marks_bot_id_as_assistant_when_user_missing():
    """Bot messages posted via chat_stream sometimes only carry bot_id."""
    raw = [
        {"type": "message", "user": "U_USER", "text": "hey", "ts": "1.0"},
        {"type": "message", "bot_id": "B_HUB", "text": "yes?", "ts": "2.0"},
    ]
    out = messages_from_thread(raw, bot_user_id="U_BOT", bot_id="B_HUB")
    assert out == [
        {"role": "user", "content": "hey"},
        {"role": "assistant", "content": "yes?"},
    ]


def test_messages_from_thread_handles_multiple_mentions():
    raw = [{"type": "message", "user": "U_USER", "text": "<@U_BOT><@U_BOT> hi", "ts": "1.0"}]
    out = messages_from_thread(raw, bot_user_id="U_BOT")
    assert out == [{"role": "user", "content": "hi"}]


# ---------------------------------------------------------------------------
# parse_sse_delta
# ---------------------------------------------------------------------------
def test_parse_sse_delta_content_chunk():
    line = b'data: {"choices":[{"delta":{"content":"hello"}}]}'
    assert parse_sse_delta(line) == "hello"


def test_parse_sse_delta_role_only_chunk():
    line = b'data: {"choices":[{"delta":{"role":"assistant"}}]}'
    assert parse_sse_delta(line) is None


def test_parse_sse_delta_done_sentinel():
    assert parse_sse_delta(b"data: [DONE]") is None


def test_parse_sse_delta_empty_line():
    assert parse_sse_delta(b"") is None


def test_parse_sse_delta_malformed_json():
    assert parse_sse_delta(b"data: {not-json}") is None


def test_parse_sse_delta_no_data_prefix():
    assert parse_sse_delta(b"event: foo") is None


def test_parse_sse_delta_accepts_string_input():
    assert parse_sse_delta('data: {"choices":[{"delta":{"content":"x"}}]}') == "x"


def test_parse_sse_delta_strips_finish_reason_chunks():
    line = b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'
    assert parse_sse_delta(line) is None


# ---------------------------------------------------------------------------
# to_slack_mrkdwn
# ---------------------------------------------------------------------------
def test_to_slack_mrkdwn_bold():
    assert to_slack_mrkdwn("**bold**") == "*bold*"
    assert to_slack_mrkdwn("**a** and **b**") == "*a* and *b*"


def test_to_slack_mrkdwn_does_not_corrupt_bold_with_inline_code():
    assert to_slack_mrkdwn("normal `code` **bold**") == "normal `code` *bold*"


def test_to_slack_mrkdwn_links():
    assert (
        to_slack_mrkdwn("see [the docs](https://hubzoid.com)")
        == "see <https://hubzoid.com|the docs>"
    )


def test_to_slack_mrkdwn_headings_become_bold():
    assert to_slack_mrkdwn("# Title\nbody") == "*Title*\nbody"
    assert to_slack_mrkdwn("## Section\nbody") == "*Section*\nbody"


def test_to_slack_mrkdwn_preserves_fenced_code_blocks():
    """Inside ``` fences, don't transform markdown — code is sacred."""
    src = "before\n```\n**not bold**\n[not link](http://x)\n```\nafter **bold**"
    out = to_slack_mrkdwn(src)
    assert "**not bold**" in out
    assert "[not link](http://x)" in out
    assert "*bold*" in out


def test_to_slack_mrkdwn_returns_empty_for_empty_input():
    assert to_slack_mrkdwn("") == ""


# ---------------------------------------------------------------------------
# truncate_for_slack
# ---------------------------------------------------------------------------
def test_truncate_for_slack_under_limit_unchanged():
    assert truncate_for_slack("hello") == "hello"


def test_truncate_for_slack_over_limit_truncated_with_marker():
    text = "x" * 50_000
    out = truncate_for_slack(text)
    assert len(out) <= 40_000
    assert out.endswith("\n\n_… response truncated to Slack's 40k char limit_")


def test_truncate_for_slack_respects_custom_limit():
    text = "x" * 500
    out = truncate_for_slack(text, limit=200)
    assert len(out) <= 200
    assert "truncated" in out
    # Most of the body should be original content.
    assert out.startswith("x" * 50)
