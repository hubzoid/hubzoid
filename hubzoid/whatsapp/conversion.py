"""Render an agent reply for WhatsApp.

WhatsApp's flavor: ``*bold*``, ``_italic_``, ``~strike~``, and ```` ```mono``` ````.
It does NOT render markdown headings, tables, or ``[label](url)`` links (bare
URLs auto-link). Body cap is 4096 chars. The shared strip steps (reasoning,
tool dropdowns) come from :mod:`hubzoid.inbound.render`; this module only adds
the WhatsApp-specific formatting and length cap. Mirrors the shape of
``hubzoid.slack.conversion`` but with WhatsApp's syntax.
"""
from __future__ import annotations

import re

from ..inbound.render import strip_thinking, strip_tool_calls, truncate

# WhatsApp text messages hard-cap at 4096 chars; a digest wants far less.
WA_TEXT_LIMIT = 4096
_TRUNCATION_MARKER = "\n\n… (truncated — ask me to continue)"

_FENCE_SPLIT_RE = re.compile(r"(```[\s\S]*?```)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_STRIKE_RE = re.compile(r"~~(.+?)~~")
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)

# A `|...|` header row followed by a `|---|...|` separator, then body rows.
_TABLE_BLOCK_RE = re.compile(
    r"""
    (?:^|\n)
    (?P<table>
        \|[^\n]*\|[ \t]*\n
        \|[ \t]*:?-+:?[ \t]*
        (?:\|[ \t]*:?-+:?[ \t]*)+
        \|[ \t]*\n
        (?:\|[^\n]*\|[ \t]*(?:\n|$))*
    )
    """,
    re.MULTILINE | re.VERBOSE,
)


def to_whatsapp(text: str) -> str:
    """Convert standard markdown into WhatsApp's flavor.

    ``**bold**`` -> ``*bold*``; ``~~strike~~`` -> ``~strike~``;
    ``# Heading`` -> ``*Heading*``; ``[label](url)`` -> ``label: url`` (bare,
    clickable); markdown tables are wrapped in a ```` ``` ```` fence so columns
    render monospace. Fenced code blocks are preserved verbatim.
    """
    if not text:
        return text
    text = _wrap_markdown_tables(text)
    out: list[str] = []
    for chunk in _FENCE_SPLIT_RE.split(text):
        if chunk.startswith("```"):
            out.append(chunk)
            continue
        chunk = _BOLD_RE.sub(r"*\1*", chunk)
        chunk = _STRIKE_RE.sub(r"~\1~", chunk)
        chunk = _HEADING_RE.sub(r"*\2*", chunk)
        chunk = _LINK_RE.sub(r"\1: \2", chunk)
        out.append(chunk)
    return "".join(out)


def _wrap_markdown_tables(text: str) -> str:
    out: list[str] = []
    for chunk in _FENCE_SPLIT_RE.split(text):
        if chunk.startswith("```"):
            out.append(chunk)
            continue

        def _wrap(match: "re.Match[str]") -> str:
            table = match.group("table").rstrip("\n")
            lead = match.group(0)[: -len(match.group("table"))]
            return f"{lead}```\n{table}\n```\n"

        out.append(_TABLE_BLOCK_RE.sub(_wrap, chunk))
    return "".join(out)


def truncate_for_whatsapp(text: str, *, limit: int = WA_TEXT_LIMIT) -> str:
    """Cap `text` to WhatsApp's per-message limit, appending a marker if cut."""
    return truncate(text, limit=limit, marker=_TRUNCATION_MARKER)


def render_final(cumulative: str) -> str:
    """Turn a complete agent reply into the WhatsApp message to send.

    WhatsApp has no streaming edit, so we render the whole thing once: strip
    reasoning and tool dropdowns, convert to WhatsApp flavor, cap the length.
    """
    visible, _active = strip_thinking(strip_tool_calls(cumulative))
    return truncate_for_whatsapp(to_whatsapp(visible.strip()))
