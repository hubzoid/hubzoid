"""Surface-agnostic render steps, shared by every chat surface.

Three things every chat surface needs, identically:
  - strip the ``<think>…</think>`` reasoning panel (Open WebUI renders it; chat
    surfaces show raw tags),
  - strip the ``<details>…</details>`` tool-call dropdown (web folds it; chat
    can't render it),
  - cap the message length.

The FLAVOR (WhatsApp's ``*bold*`` vs Telegram's MarkdownV2, links kept or
flattened) is per-surface and lives in each plugin's ``conversion`` module.
This logic matches the markers emitted by ``hubzoid.factory_claude`` and
``hubzoid.tool_events`` — the same ones ``hubzoid.slack.conversion`` handles.
"""
from __future__ import annotations

import re

_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_OPEN_RE = re.compile(r"<think>", re.IGNORECASE)
_TOOL_BLOCK_RE = re.compile(r"<details\b[^>]*>.*?</details>", re.DOTALL | re.IGNORECASE)
_TOOL_OPEN_RE = re.compile(r"<details\b[^>]*>", re.IGNORECASE)


def strip_thinking(text: str) -> "tuple[str, bool]":
    """Remove ``<think>…</think>`` reasoning from surface-bound text.

    Returns ``(visible, thinking_active)``: closed blocks are removed; an
    unclosed trailing ``<think>`` (reasoning still streaming) is dropped and
    ``thinking_active=True`` so the caller can show a "Thinking…" indicator.
    """
    if "<think>" not in text.lower():
        return text, False
    cleaned = _THINK_BLOCK_RE.sub("", text)
    m = _THINK_OPEN_RE.search(cleaned)
    if m:
        return cleaned[: m.start()], True
    return cleaned, False


def strip_tool_calls(text: str) -> str:
    """Remove compact tool-call ``<details>`` dropdowns from surface-bound text.

    Closed blocks are removed entirely; an unclosed trailing ``<details>`` (the
    dropdown still streaming) is dropped along with everything after it, so a
    half-open tag never leaks into the message.
    """
    if "<details" not in text.lower():
        return text
    cleaned = _TOOL_BLOCK_RE.sub("", text)
    m = _TOOL_OPEN_RE.search(cleaned)
    if m:
        return cleaned[: m.start()]
    return cleaned


def truncate(text: str, *, limit: int, marker: str = "") -> str:
    """If `text` exceeds `limit` chars, cut it and append `marker` within budget."""
    if len(text) <= limit:
        return text
    keep = max(0, limit - len(marker))
    return text[:keep] + marker
