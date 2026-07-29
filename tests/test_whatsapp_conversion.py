"""WhatsApp rendering flavor: WhatsApp shows `*bold*`, `_italic_`, `~strike~`,
```monospace```; it cannot render markdown headings, tables, or `[label](url)`
links (bare URLs auto-link). Cap 4096 chars. Shared strip steps come from
hubzoid.inbound.render."""
from hubzoid.whatsapp.conversion import (
    WA_TEXT_LIMIT,
    render_final,
    to_whatsapp,
    truncate_for_whatsapp,
)


def test_bold_double_to_single_asterisk():
    assert to_whatsapp("**hi**") == "*hi*"


def test_heading_becomes_bold():
    assert to_whatsapp("# Title") == "*Title*"


def test_link_flattened_to_label_and_bare_url():
    assert to_whatsapp("[report](https://x.com/r)") == "report: https://x.com/r"


def test_strikethrough_double_to_single_tilde():
    assert to_whatsapp("~~old~~") == "~old~"


def test_code_fence_content_is_not_reformatted():
    src = "```\n**not bold** [x](https://y.com)\n```"
    assert to_whatsapp(src) == src


def test_markdown_table_is_wrapped_in_monospace_fence():
    src = "| a | b |\n| - | - |\n| 1 | 2 |"
    out = to_whatsapp(src)
    assert "```" in out
    assert "| a | b |" in out


def test_truncate_caps_at_wa_limit():
    out = truncate_for_whatsapp("x" * (WA_TEXT_LIMIT + 100))
    assert len(out) <= WA_TEXT_LIMIT


def test_render_final_strips_thinking_and_tools_then_formats():
    src = "<think>reasoning</think><details>tool ran</details>**Done**"
    assert render_final(src) == "*Done*"
