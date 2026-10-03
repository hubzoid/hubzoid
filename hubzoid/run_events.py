"""Typed items in an agent run's stream.

A runtime's ``stream_events(prompt)`` yields plain ``str`` for answer text and
``RunEvent`` objects for everything else: tool calls and results, reasoning,
notices. Each event carries ``legacy``: exactly the text 1.0.x emitted for it
(tool blockquotes or ``<details>``, ``<think>`` blocks, download footers), so
text-only consumers (the OpenAI-compatible endpoint, Slack, WhatsApp, Telegram,
evals, ``hubzoid test``) keep their output by rendering ``text_of(item)``.

The Hubzoid web app renders the structured form instead (see
``hubzoid.chat.stream``). Workflows (``hub.call_agent``) keep only the answer
text, so chat presentation never leaks into a report.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Union


@dataclass(frozen=True)
class RunEvent:
    """Base class. ``legacy`` is the 1.0.x text rendering ('' renders nothing)."""

    legacy: str = field(default="", kw_only=True)


@dataclass(frozen=True)
class ToolCall(RunEvent):
    """A tool call started. ``args`` is a dict when the runtime has one."""

    id: str
    name: str
    args: Any = None


@dataclass(frozen=True)
class ToolResult(RunEvent):
    """A tool call finished. ``message`` is a short error text when ``ok`` is False.

    Result payloads are deliberately not carried: the chat shows that a tool ran
    and whether it failed, as 1.0.x did, not the raw data it returned.

    ``legacy`` is what the runtime printed for this result in 1.0.x: nothing
    for a success on every runtime, the ``format_error`` line for a Claude tool
    error, and nothing for an OpenAI Agents or Codex tool error (those runtimes
    never printed one)."""

    id: str
    name: str
    ok: bool = True
    message: str | None = None


@dataclass(frozen=True)
class ReasoningDelta(RunEvent):
    """Reasoning text. Empty ``text`` with a placeholder ``legacy`` is the
    'indicator' mode: the UI shows that the model is thinking, not what. Both
    empty means reasoning resumed inside a 1.0.x ``<think>`` block that was
    still open (nothing to print, but the UI shows thinking again)."""

    text: str = ""


@dataclass(frozen=True)
class ReasoningEnd(RunEvent):
    """The current reasoning block closed."""


@dataclass(frozen=True)
class Notice(RunEvent):
    """Text the runtime adds outside the model's answer.

    ``kind`` is 'artifacts' (download links the model did not repeat) or
    'error' (the run failed; ``text`` explains). For an error that is one of the
    plain sentences in ``hubzoid.agent_errors``, ``error_kind`` names its class
    ('usage_limit', 'auth' or 'overloaded') and ``text`` is that sentence; it is
    empty for any other error, whose ``text`` is the raw error."""

    kind: str
    text: str
    error_kind: str = ""


StreamItem = Union[str, RunEvent]

# What a failed tool call says in chat. The error itself goes to the model and
# the server log, never to the person (1.0.x showed the same sentence).
TOOL_FAILED = "The tool did not complete. The agent may retry or ask for more information."


def text_of(item: StreamItem) -> str:
    """The 1.0.x text rendering of one stream item."""
    if isinstance(item, str):
        return item
    return item.legacy


def answer_text(item: StreamItem) -> str:
    """Only the model's answer: text items, nothing else."""
    return item if isinstance(item, str) else ""


async def as_text(stream: AsyncIterator[StreamItem], *, tool_mode=None) -> AsyncIterator[str]:
    """Render native tool activity only at the legacy text boundary."""
    from .tool_events import Status, ToolActivity
    activity = ToolActivity(tool_mode) if tool_mode is not None else None
    async for item in stream:
        if activity is not None and isinstance(item, ToolCall):
            for chunk in activity.started(item.id, item.name, item.args):
                yield chunk
        elif activity is not None and isinstance(item, ToolResult):
            for chunk in activity.finished(item.id, error=not item.ok):
                yield chunk
        else:
            if activity is not None and ((isinstance(item, Notice) and item.kind == "error") or (isinstance(item, str) and bool(item))):
                for chunk in activity.flush():
                    yield chunk
            text = text_of(item)
            if isinstance(text, Status) or text:
                yield activity.text(text) if activity else text
    if activity is not None:
        for chunk in activity.flush():
            yield chunk


def stream_items(runtime: Any, prompt: str) -> AsyncIterator[StreamItem]:
    """The typed stream of one turn: ``runtime.stream_events(prompt)``, or, for a
    runtime that only has ``stream`` (a plug-in or a test double), its text as
    plain answer items."""
    events = getattr(runtime, "stream_events", None)
    if events is not None:
        return events(prompt)
    return runtime.stream(prompt)


async def answer_only(runtime: Any, prompt: str) -> str:
    """Run one turn and keep only the model's answer: no tool entries, reasoning,
    download footers or error decorations. Used where the reply is data, not
    chat (``hub.call_agent``). A runtime without ``stream_events`` returns its
    ``run`` text, which is all it can offer."""
    if getattr(runtime, "stream_events", None) is None:
        return await runtime.run(prompt)
    parts: list[str] = []
    stream = runtime.stream_events(prompt)
    try:
        async for item in stream:
            parts.append(answer_text(item))
    finally:
        await aclose(stream)
    return "".join(parts)


async def aclose(stream: Any) -> None:
    """Close an async generator if it has not finished. Never raises: this runs
    in ``finally`` blocks, where cleanup must not hide the original outcome."""
    close = getattr(stream, "aclose", None)
    if close is None:
        return
    try:
        await close()
    except Exception:  # noqa: BLE001 — best-effort cleanup
        import logging

        logging.getLogger("hubzoid.run_events").debug("stream close failed", exc_info=True)
