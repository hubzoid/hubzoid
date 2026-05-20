"""Pure data transforms used by the Slack adapter.

Kept separate from `adapter.py` so they can be exhaustively tested without
spinning up slack-bolt or talking to Slack.
"""
from __future__ import annotations

import json
import re
from typing import Any, Iterable


_MENTION_RE = re.compile(r"<@[A-Z0-9_]+>")

# Standard-markdown -> Slack mrkdwn conversions. Slack does not understand
# `**bold**`, `[label](url)`, or `# Heading`; left as-is they render as
# literal punctuation. We rewrite these. Code fences are preserved verbatim.
_FENCE_SPLIT_RE = re.compile(r"(```[\s\S]*?```)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$", re.MULTILINE)

# Slack hard caps chat.postMessage / chat.update text at 40k chars.
_SLACK_TEXT_LIMIT = 40_000
_TRUNCATION_MARKER = "\n\n_… response truncated to Slack's 40k char limit_"


def messages_from_thread(
    raw_messages: Iterable[dict[str, Any]],
    *,
    bot_user_id: str | None,
    bot_id: str | None = None,
) -> list[dict[str, str]]:
    """Turn a Slack `conversations.replies` payload into OpenAI-style messages.

    The bridge's `_flatten_messages` concatenates user/assistant turns into a
    single prompt, so the role tagging here is what gives the model thread
    context.

    Rules:
      - Bot's own messages -> {"role": "assistant"}.
      - Everyone else      -> {"role": "user"}.
      - `subtype` messages (channel_join, etc.) and empty-text messages
        are skipped — they have no semantic value.
      - `<@BOT_ID>` mention prefixes are stripped from user text so the model
        sees the actual question.
    """
    out: list[dict[str, str]] = []
    for m in raw_messages:
        if not isinstance(m, dict):
            continue
        if m.get("subtype"):
            continue
        text = (m.get("text") or "").strip()
        if not text:
            continue
        is_bot = False
        if bot_user_id and m.get("user") == bot_user_id:
            is_bot = True
        elif bot_id and m.get("bot_id") == bot_id:
            is_bot = True

        cleaned = _MENTION_RE.sub("", text).strip()
        if not cleaned:
            continue
        out.append({"role": "assistant" if is_bot else "user", "content": cleaned})
    return out


def parse_sse_delta(line: bytes | str) -> str | None:
    """Extract `choices[0].delta.content` from a single OpenAI-style SSE line.

    Returns None for: the `[DONE]` sentinel, role-only chunks, finish_reason-only
    chunks, blank lines, non-`data:` lines, and malformed JSON. This is the
    same convention `hubzoid/server.py:_stream` produces on the way out.
    """
    if isinstance(line, bytes):
        try:
            line = line.decode("utf-8")
        except UnicodeDecodeError:
            return None
    s = line.strip()
    if not s or not s.startswith("data:"):
        return None
    payload = s[len("data:"):].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        obj = json.loads(payload)
    except json.JSONDecodeError:
        return None
    try:
        delta = obj["choices"][0].get("delta") or {}
    except (KeyError, IndexError, TypeError):
        return None
    content = delta.get("content")
    if not isinstance(content, str) or not content:
        return None
    return content


def to_slack_mrkdwn(text: str) -> str:
    """Convert standard markdown into Slack's `mrkdwn` flavor.

    Conversions:
      `**bold**`         -> `*bold*`
      `[label](url)`     -> `<url|label>`
      `# Heading`        -> `*Heading*`

    Fenced code blocks (```...```) are preserved verbatim so code with
    asterisks or brackets doesn't get mangled. Inline `code` is already
    compatible with Slack and is not touched.
    """
    if not text:
        return text
    out_parts: list[str] = []
    for chunk in _FENCE_SPLIT_RE.split(text):
        if chunk.startswith("```"):
            out_parts.append(chunk)
            continue
        chunk = _BOLD_RE.sub(r"*\1*", chunk)
        chunk = _LINK_RE.sub(r"<\2|\1>", chunk)
        chunk = _HEADING_RE.sub(r"*\2*", chunk)
        out_parts.append(chunk)
    return "".join(out_parts)


def truncate_for_slack(text: str, *, limit: int = _SLACK_TEXT_LIMIT) -> str:
    """If `text` exceeds `limit` chars, cut it and append a marker.

    Slack's chat.postMessage / chat.update reject text > 40k chars. Without
    this, a long agent reply would surface as an opaque API error.
    """
    if len(text) <= limit:
        return text
    keep = max(0, limit - len(_TRUNCATION_MARKER))
    return text[:keep] + _TRUNCATION_MARKER
