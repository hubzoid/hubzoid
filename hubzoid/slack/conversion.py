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

# Markdown table detection: a `|...|` header row immediately followed by a
# `|---|...|` separator row (optionally with `:` alignment markers), then
# zero or more body rows. We wrap the whole block in a ``` fence so Slack
# at least renders it as monospace text instead of raw pipe characters.
_TABLE_BLOCK_RE = re.compile(
    r"""
    (?:^|\n)                          # start of input or new line
    (?P<table>
        \|[^\n]*\|[ \t]*\n            # header row
        \|[ \t]*:?-+:?[ \t]*          # separator: first column
        (?:\|[ \t]*:?-+:?[ \t]*)+     # separator: more columns
        \|[ \t]*\n                    # end of separator
        (?:\|[^\n]*\|[ \t]*(?:\n|$))* # body rows (zero or more)
    )
    """,
    re.MULTILINE | re.VERBOSE,
)


SLACK_FORMAT_HINT = (
    "Your reply is rendered in Slack. Slack does not display markdown tables — "
    "for tabular data, prefer bullet lists with `*label:* value` pairs over "
    "`| col | col |` rows."
)

# Slack documents chat.postMessage at 40k chars, but chat.update returns
# `msg_too_long` for `text` payloads well below that (Slack auto-promotes
# long text into block elements which max out at 3000 chars each). 3500
# is the safe envelope that works for both endpoints across workspace
# tiers without silent drops mid-stream.
_SLACK_TEXT_LIMIT = 3500
_TRUNCATION_MARKER = "\n\n_… response truncated to fit Slack's per-message limit. Ask me to continue if you need the rest._"


def messages_from_thread(
    raw_messages: Iterable[dict[str, Any]],
    *,
    bot_user_id: str | None,
    bot_id: str | None = None,
    attached_files_by_ts: dict[str, list[str]] | None = None,
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
      - If `attached_files_by_ts` is provided, each message whose `ts` is
        in the map gets `[User attached file: X. Read with read_upload('X').]`
        notes appended to its content. Empty-text messages that have
        attached files are surfaced (text-only filter is bypassed when
        attachments exist) so the agent sees the upload.
    """
    files_map = attached_files_by_ts or {}
    out: list[dict[str, str]] = []
    for m in raw_messages:
        if not isinstance(m, dict):
            continue
        if m.get("subtype"):
            continue
        text = (m.get("text") or "").strip()
        ts = m.get("ts") or ""
        attached = files_map.get(ts) or []
        if not text and not attached:
            continue
        is_bot = False
        if bot_user_id and m.get("user") == bot_user_id:
            is_bot = True
        elif bot_id and m.get("bot_id") == bot_id:
            is_bot = True

        cleaned = _MENTION_RE.sub("", text).strip()
        if attached:
            notes = "\n".join(
                f"[User attached file: {name}. Read with read_upload('{name}').]"
                for name in attached
            )
            cleaned = f"{cleaned}\n{notes}" if cleaned else notes
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
      `| col | col |...` -> ``` ... ``` (Slack does not render md tables)

    Fenced code blocks (```...```) are preserved verbatim so code with
    asterisks or brackets doesn't get mangled. Inline `code` is already
    compatible with Slack and is not touched.
    """
    if not text:
        return text
    # First pass: wrap any unfenced markdown tables in code fences. After
    # this, `_FENCE_SPLIT_RE` below sees them as code blocks and the
    # downstream conversions skip their content.
    text = _wrap_markdown_tables(text)
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


def _wrap_markdown_tables(text: str) -> str:
    """Find every markdown table block outside existing ``` fences and wrap
    it in ``` so Slack renders the columns as monospace.

    Tables inside an existing fence are left alone (already formatted as the
    model intended). Tables outside any fence get wrapped, preserving any
    surrounding prose.
    """
    out_parts: list[str] = []
    for chunk in _FENCE_SPLIT_RE.split(text):
        if chunk.startswith("```"):
            # Already a code fence — don't touch.
            out_parts.append(chunk)
            continue

        def _wrap(match: "re.Match[str]") -> str:
            table = match.group("table").rstrip("\n")
            # Preserve the leading whitespace/newline the regex consumed,
            # if any, so we don't glue the fence onto preceding prose.
            lead = match.group(0)[: -len(match.group("table"))]
            return f"{lead}```\n{table}\n```\n"

        out_parts.append(_TABLE_BLOCK_RE.sub(_wrap, chunk))
    return "".join(out_parts)


def with_slack_format_hint(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """Prepend a `role=system` formatting hint for Slack-bound replies.

    Slack-only by design: keeps the constraint out of Open WebUI / API
    consumers which DO render markdown tables correctly. Returns a new
    list — the caller's `messages` is not mutated.
    """
    return [{"role": "system", "content": SLACK_FORMAT_HINT}, *messages]


def truncate_for_slack(text: str, *, limit: int = _SLACK_TEXT_LIMIT) -> str:
    """If `text` exceeds `limit` chars, cut it and append a marker.

    Slack's chat.postMessage / chat.update reject text > 40k chars. Without
    this, a long agent reply would surface as an opaque API error.
    """
    if len(text) <= limit:
        return text
    keep = max(0, limit - len(_TRUNCATION_MARKER))
    return text[:keep] + _TRUNCATION_MARKER
