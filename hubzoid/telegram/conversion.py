"""Render an agent reply for Telegram, using HTML parse mode.

Telegram supports rich formatting and real links, so we emit HTML: ``<b>``,
``<s>``, ``<a href>``, ``<code>``, ``<pre>``. HTML mode requires ``&``, ``<`` and
``>`` in text to be escaped.

Correctness rules that keep Telegram's strict parser from rejecting the whole
message with a 400:
  - Links are stashed to placeholders BEFORE escaping/formatting, so URLs (which
    routinely contain ``_``, ``**``, ``&``) are never mangled by the inline
    passes. The href is attribute-escaped (``&`` and ``"``) on restore.
  - We do NOT convert ``_italic_``: underscores are far more common in URLs and
    identifiers than in intended italics, and converting them produces malformed
    HTML. (Same conservative stance as the WhatsApp flavor.)
  - Length is capped on the SOURCE markdown (see ``render_final``), never on the
    finished HTML, so a cut can't land mid-tag or mid-entity.
"""
from __future__ import annotations

import re

from ..inbound.render import strip_thinking, strip_tool_calls, truncate

TG_TEXT_LIMIT = 4096
_TRUNCATION_MARKER = "\n\n… (truncated — ask me to continue)"

_FENCE_SPLIT_RE = re.compile(r"(```[\s\S]*?```)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_STRIKE_RE = re.compile(r"~~(.+?)~~")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_PLACEHOLDER_RE = re.compile("\x00L(\\d+)\x00")  # NUL never appears in agent text


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _escape_attr(url: str) -> str:
    return _escape(url).replace('"', "&quot;")


def to_telegram(text: str) -> str:
    """Convert standard markdown into Telegram HTML (see module docstring)."""
    if not text:
        return text
    out: list[str] = []
    for chunk in _FENCE_SPLIT_RE.split(text):
        if chunk.startswith("```") and chunk.endswith("```") and len(chunk) >= 6:
            out.append(f"<pre>{_escape(chunk[3:-3])}</pre>")
            continue
        out.append(_format_chunk(chunk))
    return "".join(out)


def _format_chunk(chunk: str) -> str:
    links: list[tuple[str, str]] = []

    def _stash(m: "re.Match[str]") -> str:
        links.append((m.group(1), m.group(2)))
        return f"\x00L{len(links) - 1}\x00"

    chunk = _LINK_RE.sub(_stash, chunk)          # protect URLs from inline passes
    chunk = _escape(chunk)
    chunk = _BOLD_RE.sub(r"<b>\1</b>", chunk)
    chunk = _STRIKE_RE.sub(r"<s>\1</s>", chunk)
    chunk = _INLINE_CODE_RE.sub(r"<code>\1</code>", chunk)
    chunk = _HEADING_RE.sub(r"<b>\2</b>", chunk)

    def _restore(m: "re.Match[str]") -> str:
        label, url = links[int(m.group(1))]
        return f'<a href="{_escape_attr(url)}">{_escape(label)}</a>'

    return _PLACEHOLDER_RE.sub(_restore, chunk)


def truncate_for_telegram(text: str, *, limit: int = TG_TEXT_LIMIT) -> str:
    return truncate(text, limit=limit, marker=_TRUNCATION_MARKER)


def render_final(cumulative: str) -> str:
    """Turn a complete (or partial, mid-stream) agent reply into Telegram HTML."""
    visible, _active = strip_thinking(strip_tool_calls(cumulative))
    # Cap the SOURCE markdown, then convert — so a cut never lands inside a tag or
    # entity (which Telegram's HTML parser would reject with a 400).
    visible = truncate_for_telegram(visible.strip())
    return to_telegram(visible)
