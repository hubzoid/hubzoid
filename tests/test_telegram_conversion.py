"""Telegram rendering flavor: we send with HTML parse mode, which (unlike
WhatsApp) keeps links as anchors and supports bold/italic/strike/code. HTML
special chars in the text must be escaped. Cap 4096 chars."""
from hubzoid.telegram.conversion import (
    TG_TEXT_LIMIT,
    render_final,
    to_telegram,
    truncate_for_telegram,
)


def test_bold_to_html():
    assert to_telegram("**hi**") == "<b>hi</b>"


def test_underscore_italic_is_left_literal():
    # We deliberately do NOT convert _italic_: underscores are far more common in
    # URLs and identifiers, and converting them produces malformed HTML that
    # Telegram rejects with a 400. Safety over a rarely-used style.
    assert to_telegram("_hi_") == "_hi_"


def test_underscores_in_prose_stay_literal():
    assert to_telegram("call user_id then post_id") == "call user_id then post_id"


def test_link_with_underscores_in_url_is_not_mangled():
    out = to_telegram("see [the docs](https://example.com/api_v2_guide)")
    assert out == 'see <a href="https://example.com/api_v2_guide">the docs</a>'
    assert "<i>" not in out


def test_link_url_quote_and_amp_are_escaped_in_href():
    out = to_telegram('[x](https://e.com/a?b=1&c=2)')
    assert 'href="https://e.com/a?b=1&amp;c=2"' in out


def test_bold_inside_url_not_applied():
    out = to_telegram("[x](https://e.com/a**b**c)")
    assert "<b>" not in out
    assert 'https://e.com/a**b**c' in out


def test_link_kept_as_anchor():
    assert to_telegram("[report](https://x.com/r)") == '<a href="https://x.com/r">report</a>'


def test_heading_becomes_bold():
    assert to_telegram("# Title") == "<b>Title</b>"


def test_html_special_chars_are_escaped():
    assert to_telegram("a < b & c > d") == "a &lt; b &amp; c &gt; d"


def test_code_fence_content_is_escaped_inside_pre():
    # The opening fence line (info string, empty here) is dropped, so no leading blank line.
    assert to_telegram("```\n<x>&\n```") == "<pre>&lt;x&gt;&amp;\n</pre>"


def test_code_fence_language_tag_is_dropped():
    # code-review #8: a ```python opener must not leak "python" as the first code line.
    assert to_telegram("```python\nprint(1)\n```") == "<pre>print(1)\n</pre>"


def test_single_line_fence_kept_as_is():
    # An inline-style fence with no newline has no info string to strip.
    assert to_telegram("```code```") == "<pre>code</pre>"


def test_truncate_caps_at_tg_limit():
    assert len(truncate_for_telegram("x" * (TG_TEXT_LIMIT + 50))) <= TG_TEXT_LIMIT


def test_render_final_strips_thinking_and_tools_then_formats():
    assert render_final("<think>r</think><details>t</details>**Done**") == "<b>Done</b>"
