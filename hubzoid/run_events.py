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
    and whether it failed, as 1.0.x did, not the raw data it returned."""

    id: str
    name: str
    ok: bool = True
    message: str | None = None


@dataclass(frozen=True)
class ReasoningDelta(RunEvent):
    """Reasoning text. Empty ``text`` with a placeholder ``legacy`` is the
    'indicator' mode: the UI shows that the model is thinking, not what."""

    text: str = ""


@dataclass(frozen=True)
class ReasoningEnd(RunEvent):
    """The current reasoning block closed."""


@dataclass(frozen=True)
class Notice(RunEvent):
    """Text the runtime adds outside the model's answer.

    ``kind`` is 'artifacts' (download links the model did not repeat) or
    'error' (the run failed; ``text`` explains)."""

    kind: str
    text: str


StreamItem = Union[str, RunEvent]


def text_of(item: StreamItem) -> str:
    """The 1.0.x text rendering of one stream item."""
    if isinstance(item, str):
        return item
    return item.legacy


def answer_text(item: StreamItem) -> str:
    """Only the model's answer: text items, nothing else."""
    return item if isinstance(item, str) else ""


async def as_text(stream: AsyncIterator[StreamItem]) -> AsyncIterator[str]:
    """Adapt a typed stream to the 1.0.x text stream."""
    async for item in stream:
        text = text_of(item)
        if text:
            yield text
