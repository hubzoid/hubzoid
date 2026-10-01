"""One run's items as the AI SDK UI message stream (v1) and as stored content.

``MessageBuilder`` consumes a runtime's typed stream (``hubzoid.run_events``)
and returns, for each item, the UI stream chunks to send (contract 6.3) while it
builds the message's content parts for the database (contract 6.4). Both come
from the same state, so a reloaded page shows what the stream showed.

  * Consecutive answer text merges into one text part (``t1``, ``t2`` ...) until
    a reasoning block or a tool call interrupts it.
  * Reasoning becomes a reasoning part (``r1`` ...). In the indicator mode its
    text is empty: the UI shows that the agent was thinking, not what.
  * A tool call becomes a ``tool-call`` part whose ``result`` is filled when the
    result arrives (``{"status": "ok"}`` or ``{"status": "error", "message"}``).
    Arguments are kept small (long values are cut).
  * The download footer (``Notice`` 'artifacts') is answer text: markdown links.
  * A run error (``Notice`` 'error') is an ``error`` chunk and the message's
    ``error``; it never becomes answer text.

``encode`` writes one chunk as an SSE ``data:`` line.
"""
from __future__ import annotations

import copy
import json
from typing import Any

from .. import run_events
from ..run_events import Notice, ReasoningDelta, ReasoningEnd, ToolCall, ToolResult

HEADERS = {
    "x-vercel-ai-ui-message-stream": "v1",
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
}
DONE = b"data: [DONE]\n\n"
# An SSE comment: ignored by parsers, keeps proxies from closing an idle stream.
KEEPALIVE = b": keep-alive\n\n"

TOOL_FAILED = run_events.TOOL_FAILED
_STOPPED = "Stopped before this step finished."
_RUN_FAILED = "The run failed before this step finished."

# Tool arguments shown in the chat are for recognition, not a data dump.
_ARG_STRING_MAX = 2000
_ARGS_JSON_MAX = 16_000


def encode(chunk: dict) -> bytes:
    return ("data: " + json.dumps(chunk, ensure_ascii=False, separators=(",", ":"))
            + "\n\n").encode("utf-8")


def _shorten(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= _ARG_STRING_MAX else value[:_ARG_STRING_MAX] + "…"
    if isinstance(value, dict):
        return {str(k): _shorten(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_shorten(v) for v in value]
    return value


def tool_input(args: Any) -> dict:
    """The arguments of a call as a small JSON object."""
    if isinstance(args, str):
        text = args.strip()
        if not text:
            args = {}
        else:
            try:
                args = json.loads(text)
            except ValueError:
                args = {"input": text}
    if args is None:
        args = {}
    if not isinstance(args, dict):
        args = {"input": args}
    safe = json.loads(json.dumps(_shorten(args), default=str))
    encoded = json.dumps(safe, ensure_ascii=False)
    if len(encoded) > _ARGS_JSON_MAX:
        return {"preview": encoded[:_ARG_STRING_MAX] + "…"}
    return safe


class MessageBuilder:
    """Build one assistant message's stream chunks and stored parts."""

    def __init__(self, message_id: str, conversation_id: str, *, show_tools: bool = True):
        self.message_id = message_id
        self.conversation_id = conversation_id
        self.show_tools = show_tools
        self.parts: list[dict] = []
        self.error: str | None = None
        self.finished = False
        self._error_sent = False
        self._text: tuple[int, str] | None = None       # (part index, stream id)
        self._reasoning: tuple[int, str] | None = None
        self._counts = {"t": 0, "r": 0, "c": 0}
        self._calls: dict[str, int] = {}                # open call id -> part index
        self._runtime_ids: dict[str, str] = {}          # runtime call id -> stream id
        self._used_ids: set[str] = set()

    # -- lifecycle ---------------------------------------------------------------
    def start(self) -> list[dict]:
        return [
            {"type": "start", "messageId": self.message_id,
             "messageMetadata": {"conversationId": self.conversation_id}},
            {"type": "start-step"},
        ]

    def title(self, title: str) -> list[dict]:
        return [{"type": "data-title", "data": {"title": title}, "transient": True}]

    def feed(self, item: Any) -> list[dict]:
        if self.finished:
            return []
        if isinstance(item, str):
            return self._append_text(item)
        if isinstance(item, ReasoningDelta):
            return self._append_reasoning(item.text)
        if isinstance(item, ReasoningEnd):
            return self._close_reasoning()
        if isinstance(item, ToolCall):
            return self._tool_call(item) if self.show_tools else []
        if isinstance(item, ToolResult):
            return self._tool_result(item) if self.show_tools else []
        if isinstance(item, Notice):
            if item.kind == "error":
                return self._error(item.text or "The agent could not complete this reply.")
            return self._append_text(item.legacy or item.text)
        return []

    def fail(self, message: str) -> list[dict]:
        """A failure outside the runtime's own stream (an exception)."""
        return [] if self.finished else self._error(message)

    def finish(self, status: str) -> list[dict]:
        """Close every open block and end the message with ``status``
        ('complete', 'cancelled' or 'error')."""
        if self.finished:
            return []
        chunks = self._close_blocks()
        for call_id, index in list(self._calls.items()):
            part = self.parts[index]
            if status == "complete":
                part["result"] = {"status": "ok"}
                chunks.append({"type": "tool-output-available", "toolCallId": call_id,
                               "output": {"status": "ok"}})
            else:
                message = _STOPPED if status == "cancelled" else _RUN_FAILED
                part["result"] = {"status": "error", "message": message}
                chunks.append({"type": "tool-output-error", "toolCallId": call_id,
                               "errorText": message})
        self._calls.clear()
        if status == "error" and not self._error_sent:
            chunks += self._error(self.error or "The agent could not complete this reply.")
        chunks += [{"type": "finish-step"},
                   {"type": "finish", "messageMetadata": {"status": status}}]
        self.finished = True
        return chunks

    # -- stored form -------------------------------------------------------------
    def snapshot(self) -> list[dict]:
        return copy.deepcopy(self.parts)

    def plain_text(self) -> str:
        return "".join(p.get("text", "") for p in self.parts if p.get("type") == "text")

    # -- blocks ------------------------------------------------------------------
    def _next(self, kind: str) -> str:
        self._counts[kind] += 1
        return f"{kind}{self._counts[kind]}"

    def _append_text(self, delta: str) -> list[dict]:
        if not delta:
            return []
        chunks = self._close_reasoning()
        if self._text is None:
            sid = self._next("t")
            self.parts.append({"type": "text", "text": ""})
            self._text = (len(self.parts) - 1, sid)
            chunks.append({"type": "text-start", "id": sid})
        index, sid = self._text
        self.parts[index]["text"] += delta
        chunks.append({"type": "text-delta", "id": sid, "delta": delta})
        return chunks

    def _close_text(self) -> list[dict]:
        if self._text is None:
            return []
        _, sid = self._text
        self._text = None
        return [{"type": "text-end", "id": sid}]

    def _append_reasoning(self, delta: str) -> list[dict]:
        chunks = self._close_text()
        if self._reasoning is None:
            sid = self._next("r")
            self.parts.append({"type": "reasoning", "text": ""})
            self._reasoning = (len(self.parts) - 1, sid)
            chunks.append({"type": "reasoning-start", "id": sid})
        index, sid = self._reasoning
        if delta:
            self.parts[index]["text"] += delta
            chunks.append({"type": "reasoning-delta", "id": sid, "delta": delta})
        return chunks

    def _close_reasoning(self) -> list[dict]:
        if self._reasoning is None:
            return []
        _, sid = self._reasoning
        self._reasoning = None
        return [{"type": "reasoning-end", "id": sid}]

    def _close_blocks(self) -> list[dict]:
        return self._close_text() + self._close_reasoning()

    # -- tools -------------------------------------------------------------------
    def _tool_call(self, item: ToolCall) -> list[dict]:
        chunks = self._close_blocks()
        runtime_id = str(item.id or "")
        call_id = runtime_id if runtime_id and runtime_id not in self._used_ids else self._next("c")
        while call_id in self._used_ids:
            call_id = self._next("c")
        self._used_ids.add(call_id)
        if runtime_id:
            self._runtime_ids[runtime_id] = call_id
        args = tool_input(item.args)
        self.parts.append({"type": "tool-call", "toolCallId": call_id, "toolName": item.name,
                           "args": args})
        self._calls[call_id] = len(self.parts) - 1
        chunks.append({"type": "tool-input-available", "toolCallId": call_id,
                       "toolName": item.name, "input": args})
        return chunks

    def _tool_result(self, item: ToolResult) -> list[dict]:
        call_id = self._runtime_ids.get(str(item.id or ""))
        if call_id not in self._calls:
            # No id to match: the latest open call of that tool, else the latest.
            open_calls = list(self._calls)
            named = [c for c in open_calls if self.parts[self._calls[c]]["toolName"] == item.name]
            call_id = (named or open_calls or [None])[-1]
        if call_id is None:
            return []
        part = self.parts[self._calls.pop(call_id)]
        if item.ok:
            part["result"] = {"status": "ok"}
            return [{"type": "tool-output-available", "toolCallId": call_id,
                     "output": {"status": "ok"}}]
        message = item.message or TOOL_FAILED
        part["result"] = {"status": "error", "message": message}
        return [{"type": "tool-output-error", "toolCallId": call_id, "errorText": message}]

    # -- errors ------------------------------------------------------------------
    def _error(self, message: str) -> list[dict]:
        chunks = self._close_blocks()
        self.error = message
        self._error_sent = True
        chunks.append({"type": "error", "errorText": message})
        return chunks
