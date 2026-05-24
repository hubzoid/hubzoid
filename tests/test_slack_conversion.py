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
# Markdown table auto-wrap (Slack does not render | tables natively)
# ---------------------------------------------------------------------------
def test_to_slack_mrkdwn_wraps_markdown_table_in_code_fence():
    """Models reach for tables on structured comparisons. Slack mrkdwn just
    renders them as raw pipe characters with no column alignment. Wrapping
    in ``` makes the model's intent at least readable (monospace columns)."""
    src = (
        "Here is a comparison:\n"
        "| name | value |\n"
        "|------|-------|\n"
        "| a    | 1     |\n"
        "| b    | 2     |\n"
        "After table."
    )
    out = to_slack_mrkdwn(src)
    assert "```\n| name | value |" in out
    assert "| b    | 2     |\n```" in out
    # Prose around the table is unchanged.
    assert "Here is a comparison:\n" in out
    assert "After table." in out


def test_to_slack_mrkdwn_wraps_table_with_alignment_markers():
    """Separator row with `:---:` / `:---` / `---:` still recognised."""
    src = (
        "| col1 | col2 |\n"
        "|:-----|-----:|\n"
        "| x    | y    |"
    )
    out = to_slack_mrkdwn(src)
    assert out.startswith("```\n")
    assert out.rstrip().endswith("```")


def test_to_slack_mrkdwn_lone_pipe_line_is_not_wrapped():
    """A pipe character in prose without a separator row is not a table."""
    src = "use `foo | bar` to pipe output."
    out = to_slack_mrkdwn(src)
    assert "```" not in out


def test_to_slack_mrkdwn_does_not_double_wrap_table_already_in_fence():
    """If the model already wrapped a table in ``` for us, leave it alone."""
    src = "```\n| a | b |\n|---|---|\n| 1 | 2 |\n```"
    out = to_slack_mrkdwn(src)
    # Exactly one opening + one closing fence — no double-wrap.
    assert out.count("```") == 2


def test_to_slack_mrkdwn_handles_multiple_tables_in_one_message():
    src = (
        "First:\n| a | b |\n|---|---|\n| 1 | 2 |\n"
        "between\n"
        "Second:\n| c | d |\n|---|---|\n| 3 | 4 |"
    )
    out = to_slack_mrkdwn(src)
    assert out.count("```") == 4  # two opens + two closes
    assert "between" in out


def test_to_slack_mrkdwn_does_not_mangle_bold_inside_table_after_wrap():
    """Once a table is fenced, asterisks inside it must stay literal."""
    src = (
        "| name | desc |\n"
        "|------|------|\n"
        "| x    | **important** |"
    )
    out = to_slack_mrkdwn(src)
    # The `**important**` must NOT have been converted to `*important*`
    # because it's now inside a fence.
    assert "**important**" in out


# ---------------------------------------------------------------------------
# with_slack_format_hint
# ---------------------------------------------------------------------------
def test_with_slack_format_hint_prepends_system_message():
    from hubzoid.slack.conversion import with_slack_format_hint

    msgs = [{"role": "user", "content": "hi"}]
    out = with_slack_format_hint(msgs)
    assert len(out) == 2
    assert out[0]["role"] == "system"
    assert out[1] == msgs[0]


def test_with_slack_format_hint_mentions_slack_and_bullets():
    from hubzoid.slack.conversion import with_slack_format_hint

    out = with_slack_format_hint([{"role": "user", "content": "hi"}])
    hint = out[0]["content"]
    # Names the surface so the model understands the constraint.
    assert "Slack" in hint
    # Steers toward bullet alternative for tabular data.
    assert "bullet" in hint.lower() or "list" in hint.lower()
    assert "table" in hint.lower()


def test_with_slack_format_hint_returns_new_list_not_mutating_input():
    from hubzoid.slack.conversion import with_slack_format_hint

    msgs = [{"role": "user", "content": "hi"}]
    _ = with_slack_format_hint(msgs)
    assert len(msgs) == 1


# ---------------------------------------------------------------------------
# truncate_for_slack
# ---------------------------------------------------------------------------
def test_truncate_for_slack_under_limit_unchanged():
    assert truncate_for_slack("hello") == "hello"


def test_truncate_for_slack_over_limit_truncated_with_marker():
    """Default cap is Slack's safe envelope (3500 chars). Anything longer
    gets cut with a marker so we never trip msg_too_long mid-stream."""
    text = "x" * 50_000
    out = truncate_for_slack(text)
    assert len(out) <= 3500
    assert "truncated" in out


def test_truncate_for_slack_respects_custom_limit():
    text = "x" * 500
    out = truncate_for_slack(text, limit=200)
    assert len(out) <= 200
    assert "truncated" in out
    # Most of the body should be original content.
    assert out.startswith("x" * 50)
