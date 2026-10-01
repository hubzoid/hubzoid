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
    > ↳ **tool_name** `arg1=value1 arg2=value2`

Errors get a separate ⚠ line because the agent's reply may not always
surface the failure clearly:

    > ⚠ **tool_name** {error message}

There is deliberately no per-frontend protocol. One text format, every
frontend gets the same information.

``ToolActivity`` is what the runtimes use per turn. In the default compact
mode it writes each call, when it finishes, as an Open WebUI tool-call block
(``<details type="tool_calls" ...>``): the chat app shows a ✓ or ✗ row and folds
consecutive tool calls and thinking into one group. While a call runs it
yields a ``Status`` ("Running <tool>…"), which the bridge sends as the chat
app's status line, never as message text. Slack and the messaging surfaces
strip ``<details …>`` blocks, as before.
"""
from __future__ import annotations

import html
import json

# How long an argument JSON we will inline alongside the tool name. Keep
# this short — the goal is to identify the call, not to dump payloads.
_ARG_PREVIEW_MAX = 80


def format_call(name: str, args: object | None = None, *, mode: str = "full") -> str:
    """One row per tool call, emitted at call start. No matching "returned" line.

    `mode` (the ``SHOW_TOOLS`` setting) chooses the rendering:

      * ``full``    -> legacy inline blockquote ``> ↳ **tool_name** `args``` .
                       Shown verbatim on every surface (verbose / debug).
      * ``compact`` -> a collapsible ``<details>`` dropdown: a short ``↳ name``
                       summary the web UI folds, with the args in the body
                       (revealed on expand). The Slack adapter strips it.
      * ``off``     -> emit nothing (returns ``""``).

    `args` may be a dict (most callers), a JSON-stringified body (Claude
    SDK), or None (no preview).
    """
    if mode == "off":
        return ""
    preview = _preview(args)
    if mode == "compact":
        summary = f"↳ {_escape(name)}"
        body = f"`{preview}`" if preview else "_(no arguments)_"
        return f"\n\n<details>\n<summary>{summary}</summary>\n\n{body}\n\n</details>\n\n"
    body = f"**{_escape(name)}**"
    if preview:
        body = f"{body} `{preview}`"
    return f"\n\n> ↳ {body}\n\n"


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
    seen: set[str] = set()
    for art in artifacts:
        url = (art or {}).get("url")
        name = (art or {}).get("name") or "file"
        # Skip blanks, links the model already echoed, and repeats — the same
        # file written more than once this turn shares one URL.
        if not url or url in shown_text or url in seen:
            continue
        seen.add(url)
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
    message = message or "The tool did not complete. The agent may retry or ask for more information."
    if message:
        first_line = message.splitlines()[0][:120]
        body = f"{body} {_escape(first_line)}"
    return f"\n\n> ⚠ {body}\n\n"


class Status(str):
    """A status-line update, not message text: an empty string carrying a
    `description` (None clears the line). Every consumer that joins or tests
    the stream's text sees nothing; the bridge's stream turns it into an
    Open WebUI status event."""

    description: str | None

    def __new__(cls, description: str | None) -> "Status":
        obj = super().__new__(cls, "")
        obj.description = description
        return obj


#: The status line while the model thinks between tool rounds.
THINKING = "Thinking…"

# Longest string value, and whole argument JSON, written into a tool block.
# Small on purpose: the chat app sends earlier replies back with the next
# message, so every block also lands in later prompts.
_BLOCK_VALUE_MAX = 120
_BLOCK_ARGS_MAX = 600
FAILED_TEXT = "The tool did not complete. The agent may retry or ask for more information."


def format_tool_block(call_id: str, name: str, args: object | None = None, *,
                      error: bool = False, follows_block: bool = False) -> str:
    """One finished tool call as an Open WebUI tool-call block.

    The chat app shows it as a row with a ✓ (or ✗ when `error`) and the input
    on expand. It folds blocks that touch into one "Explored …" group: a
    block ends with exactly one newline, and the next one (`follows_block`)
    starts right after it; two newlines would split the group. The first of a
    run starts on a new paragraph. The result is never written (it can be
    large or private); a failed call carries the short failure text instead."""
    attrs = {"type": "tool_calls", "done": "true", "id": call_id or "", "name": _escape(name),
             "arguments": _args_json(args)}
    if error:
        attrs["status"] = "failed"
    rendered = " ".join(f'{k}="{html.escape(str(v), quote=True)}"' for k, v in attrs.items())
    body = f"{FAILED_TEXT}\n" if error else ""
    lead = "" if follows_block else "\n\n"
    return f"{lead}<details {rendered}>\n<summary>Tool Executed</summary>\n{body}</details>\n"


# Tool output that means the call did not do its job, on every runtime: the
# Agents SDK's text for a tool that raised, and the access guard's refusal.
_FAILURE_PREFIXES = ("An error occurred while running the tool", "[access denied:")


def failed_output(output: object) -> bool:
    """Whether a tool's returned output reports a failure (shown as ✗)."""
    if isinstance(output, list):  # content blocks: [{"type": "text", "text": ...}]
        output = "".join(b.get("text", "") for b in output if isinstance(b, dict))
    return isinstance(output, str) and output.lstrip().startswith(_FAILURE_PREFIXES)


def running_status(names: list[str]) -> Status:
    """"Running a…", "Running a and b…", "Running a, b and 3 more…" in start
    order (parallel calls can report together, so no one name is "the" slow
    one), or a cleared line when nothing runs."""
    names = [_escape(n) for n in names]
    if not names:
        return Status(None)
    if len(names) <= 2:
        return Status(f"Running {' and '.join(names)}…")
    return Status(f"Running {names[0]}, {names[1]} and {len(names) - 2} more…")


class ToolActivity:
    """Tool-call display for one turn, by SHOW_TOOLS mode.

      * compact -> `Status("Running <tool>…")` when a call starts; the tool
                   block when it finishes, then the next running call's
                   status or a cleared one.
      * full    -> the legacy inline `> ↳` line at the start and a `> ⚠` line
                   on error.
      * off     -> nothing.

    Every method returns chunks to yield in order (text or `Status`). Runtimes
    pass the turn's other visible text through `text()`, so a block knows
    whether it continues a run of blocks."""

    def __init__(self, mode: str = "compact"):
        self.mode = mode
        self._pending: dict[str, tuple[str, object]] = {}
        self._names: dict[str, str] = {}
        self._seq = 0
        self._after_block = False

    def text(self, chunk: str) -> str:
        """Visible text (answer or thinking) the runtime is about to yield."""
        if chunk:
            self._after_block = False
        return chunk

    def _block(self, call_id, name, args, *, error=False) -> str:
        block = format_tool_block(call_id, name, args, error=error, follows_block=self._after_block)
        self._after_block = True
        return block

    def started(self, call_id: str | None, name: str, args: object | None = None) -> list[str]:
        if self.mode == "off":
            return []
        self._seq += 1
        key = call_id or f"call-{self._seq}"
        self._names[key] = name
        if self.mode == "full":
            return [format_call(name, args, mode="full")]
        self._pending[key] = (name, args)
        return [self._status()]

    def finished(self, call_id: str | None, *, error: bool = False) -> list[str]:
        if self.mode == "off":
            return []
        if self.mode == "full":
            name = self._names.get(call_id or "", "tool")
            return [format_error(name)] if error else []
        if call_id not in self._pending:
            return []
        name, args = self._pending.pop(call_id)
        return [self._block(call_id, name, args, error=error), self._status()]

    def flush(self) -> list[str]:
        """Write calls that never reported a result (the turn ended or failed)
        and clear the status line."""
        if self.mode != "compact" or not self._pending:
            return []
        out = [self._block(k, n, a) for k, (n, a) in self._pending.items()]
        self._pending.clear()
        return out + [Status(None)]

    def _status(self) -> Status:
        return running_status([n for n, _ in self._pending.values()])


# ---------------------------------------------------------------------------
# Internal helpers.
# ---------------------------------------------------------------------------
def _args_json(args: object | None) -> str:
    """The call's arguments as JSON for the block's input view, long string
    values shortened so a big payload (a whole report) never lands in chat."""
    if args is None:
        return ""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return args[:_BLOCK_ARGS_MAX]

    def short(v):
        if isinstance(v, str) and len(v) > _BLOCK_VALUE_MAX:
            return v[:_BLOCK_VALUE_MAX - 1] + "…"
        if isinstance(v, dict):
            return {k: short(x) for k, x in v.items()}
        if isinstance(v, list):
            return [short(x) for x in v[:20]]
        return v

    text = json.dumps(short(args), ensure_ascii=False, default=str)
    return text if len(text) <= _BLOCK_ARGS_MAX else text[:_BLOCK_ARGS_MAX - 1] + "…"


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
