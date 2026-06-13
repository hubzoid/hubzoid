"""Format tool activity as inline status messages for the chat stream.

One line per tool call. Emitted at call start; no separate result/confirm
line. The wire format is a markdown blockquote so every consumer renders
it sensibly:

  * Open WebUI         renders `> ...` as a quoted line with a vertical
                       bar — looks like a status indicator above the next
                       reply chunk.
  * Slack mrkdwn       `>` is blockquote — adapter passes through.
  * curl / SDK / logs  still readable as plain text; greppable with the
                       leading `> ` prefix.

Format:
    > ✓ **tool_name** `arg1=value1 arg2=value2`

Errors get a separate ⚠ line because the agent's reply may not always
surface the failure clearly:

    > ⚠ **tool_name** {error message}

There is deliberately no per-frontend protocol. One text format, every
frontend gets the same information.
"""
from __future__ import annotations

# How long an argument JSON we will inline alongside the tool name. Keep
# this short — the goal is to identify the call, not to dump payloads.
_ARG_PREVIEW_MAX = 80


def format_call(name: str, args: object | None = None) -> str:
    """Single line per call. Format: ``> ✓ **tool_name** `args``` .

    Emitted at call start. There is no matching "returned" line — the
    user sees one row per tool invocation, then the model's reply.

    `args` may be a dict (most callers), a JSON-stringified body (Claude
    SDK), or None (no preview).
    """
    preview = _preview(args)
    body = f"**{_escape(name)}**"
    if preview:
        body = f"{body} `{preview}`"
    return f"\n\n> ✓ {body}\n\n"


def format_artifact_footer(artifacts: list, shown_text: str = "") -> str:
    """Append download links the model did not surface itself.

    The model is not required to repeat a `write_artifact` link; the runtime
    drains the per-request registry (`_request_ctx.drain_artifacts`) at end of
    turn and passes the entries here so the link reaches the user on every
    backend and surface. Links whose URL already appears in `shown_text` (the
    model echoed it) are skipped so we never double-post. The markdown link
    format is what the Slack adapter rewrites to `<url|label>` mrkdwn.
    """
    if not artifacts:
        return ""
    lines = []
    for art in artifacts:
        url = (art or {}).get("url")
        name = (art or {}).get("name") or "file"
        if not url or url in shown_text:
            continue
        lines.append(f"[Download {name}]({url})")
    if not lines:
        return ""
    return "\n\n" + "\n".join(lines) + "\n"


def format_error(name: str, message: str | None = None) -> str:
    """`> ⚠ **tool_name** {short error}` — emitted when a tool errors.

    Errors get a separate line because the agent's reply may not always
    surface the failure clearly.
    """
    body = f"**{_escape(name)}**"
    if message:
        first_line = message.splitlines()[0][:120]
        body = f"{body} {_escape(first_line)}"
    return f"\n\n> ⚠ {body}\n\n"


# ---------------------------------------------------------------------------
# Internal helpers.
# ---------------------------------------------------------------------------
def _preview(args: object | None) -> str:
    if args is None:
        return ""
    if isinstance(args, str):
        text = args.strip()
    elif isinstance(args, dict):
        # Show the first 1-2 key=value pairs; the model usually puts the
        # interesting bit first (e.g. read_knowledge name='jexl').
        bits = []
        for k, v in args.items():
            v_short = repr(v) if not isinstance(v, str) else v
            if len(v_short) > 40:
                v_short = v_short[:37] + "…"
            bits.append(f"{k}={v_short}")
            if len(bits) >= 2:
                break
        text = " ".join(bits)
    else:
        text = str(args)
    text = text.replace("\n", " ").replace("`", "")
    if len(text) > _ARG_PREVIEW_MAX:
        text = text[: _ARG_PREVIEW_MAX - 1] + "…"
    return text


def _escape(text: str) -> str:
    """Strip backtick characters so we don't break the markdown code spans."""
    return text.replace("`", "")


# ---------------------------------------------------------------------------
# Tool-name normalisation. Both backends prefix their tool names; the user
# does not care that the read_knowledge call landed at
# `mcp__hubzoid__read_knowledge`. Strip the noise.
# ---------------------------------------------------------------------------
_STRIP_PREFIXES = ("mcp__hubzoid__",)


def short_name(raw: str) -> str:
    for p in _STRIP_PREFIXES:
        if raw.startswith(p):
            return raw[len(p):]
    return raw
