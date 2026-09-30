"""A deterministic runtime for tests: no model, no network, no cost.

Selected by ``runtime.build`` when the hub's model is ``hubzoid-test/<anything>``
AND ``HUBZOID_TEST_RUNTIME=1``. Without the flag, the model id is refused with a
clear error, so a test setting can never answer real people by accident.

A reply is chosen by keywords (whole words, any case) in the latest user
message. Keywords combine; the parts run in this order:

  think     reasoning first (full text, an indicator or nothing, per SHOW_THINKING)
  tool      one tool call (``read_knowledge``) that succeeds
  fail      one tool call (``grep_data``) that fails
  artifact  one ``write_artifact`` call that saves a small file in the chat's
            artifact folder and registers its link, so the download footer shows
  markdown  a reply with a table and a code block
  slow      about 40 text chunks over about 20 seconds (for cancellation);
            ``HUBZOID_TEST_SLOW_SECONDS`` changes the duration
  error     some text, then the run fails

With none of them, the reply is short markdown that quotes the request and lists
any attached files. Usage (token counts from word counts, zero cost) is recorded
through ``_request_ctx.note_usage`` like a real backend's.

``complete`` answers ``runtime.complete_once`` for these models: a short title
made from the message, or a small JSON object.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import AsyncIterator

from . import _request_ctx, run_events, tool_events
from .run_events import Notice, ReasoningDelta, ReasoningEnd, StreamItem, ToolCall, ToolResult

log = logging.getLogger("hubzoid.testing_runtime")

MODEL_PREFIX = "hubzoid-test/"
ENV_FLAG = "HUBZOID_TEST_RUNTIME"
KEYWORDS = ("think", "tool", "fail", "artifact", "markdown", "slow", "error")

_WORD = re.compile(r"[A-Za-z]+")
_NOTE = re.compile(r"^\[(?:Image|User attached file): ([^\](]+?)\s*(?:\(|\])", re.MULTILINE)


def is_test_model(model_id: str | None) -> bool:
    return (model_id or "").strip().lower().startswith(MODEL_PREFIX)


def require_enabled(model_id: str) -> None:
    if (os.environ.get(ENV_FLAG) or "").strip() != "1":
        raise RuntimeError(
            f"MODEL={model_id} selects Hubzoid's scripted test runtime, which never calls "
            f"a model. It runs only with {ENV_FLAG}=1 (tests and demos). Set a real "
            "model for people to use."
        )


def build(hub_dir: Path, *, model_id: str, settings) -> "ScriptedRuntime":
    require_enabled(model_id)
    from .loaders import agents as agents_loader

    try:
        name = agents_loader.load_main(hub_dir).spec.name
    except Exception:  # noqa: BLE001 — a broken AGENTS.md still gets a runtime
        name = Path(hub_dir).name
    return ScriptedRuntime(hub_dir, name=name, model_id=model_id,
                           tool_mode=settings.show_tools, thinking_mode=settings.thinking_mode)


def latest_request(prompt: str) -> str:
    """The latest user message of a flattened prompt (``[user]`` blocks), or the
    whole prompt when it has none."""
    marker = "[user]\n"
    return prompt.rsplit(marker, 1)[-1] if marker in prompt else prompt


def _without_notes(text: str) -> str:
    """The message text without the attachment notes the bridge adds."""
    return "\n".join(line for line in (text or "").splitlines()
                     if not line.startswith(("[Image:", "[User attached file:",
                                             "[Attachment unreadable:")))


def keywords(text: str) -> set[str]:
    words = {w.lower() for w in _WORD.findall(_without_notes(text))}
    return {k for k in KEYWORDS if k in words}


def _quote(text: str, limit: int = 60) -> str:
    plain = " ".join(_without_notes(text).split())
    if len(plain) <= limit:
        return plain
    cut = plain[:limit].rsplit(" ", 1)[0] or plain[:limit]
    return cut + "…"


def _chunks(text: str) -> list[str]:
    """Split text into word-sized pieces that join back to it exactly."""
    return re.findall(r"\S+\s*|\s+", text)


_MARKDOWN_REPLY = (
    "Here is a summary table:\n\n"
    "| Item | Count |\n"
    "|---|---:|\n"
    "| Apples | 3 |\n"
    "| Pears | 5 |\n\n"
    "And a code sample:\n\n"
    "```python\n"
    "def total(counts):\n"
    "    return sum(counts)\n"
    "```\n"
)

ARTIFACT_NAME = "scripted-report.md"


class ScriptedRuntime:
    """Runtime protocol (see ``hubzoid.runtime``) with scripted replies."""

    def __init__(self, hub_dir: Path, *, name: str, model_id: str,
                 tool_mode: str = "compact", thinking_mode: str = "indicator"):
        self.hub_dir = Path(hub_dir)
        self.name = name
        self.model_id = model_id
        self._tool_mode = tool_mode
        self._thinking_mode = thinking_mode
        # Set when the last run failed, as on the other runtimes (run_once raises).
        self.last_error: BaseException | None = None

    async def aopen(self) -> None:
        """Nothing to connect."""

    async def aclose(self) -> None:
        """Nothing to close."""

    def stream(self, prompt: str) -> AsyncIterator[str]:
        return run_events.as_text(self.stream_events(prompt))

    async def run(self, prompt: str) -> str:
        return "".join([part async for part in self.stream(prompt)])

    async def stream_events(self, prompt: str) -> AsyncIterator[StreamItem]:
        from .factory_claude import _ThinkStream

        self.last_error = None
        request = latest_request(prompt)
        words = keywords(request)
        shown: list[str] = []
        think = _ThinkStream(self._thinking_mode)
        output_words = 0

        def text(piece: str) -> str:
            nonlocal output_words
            shown.append(piece)
            output_words += len(piece.split())
            return piece

        if "think" in words and self._thinking_mode != "off":
            for piece in ("Considering ", "the request ", "step by step."):
                shown_text = piece if self._thinking_mode == "full" else ""
                legacy = think.thinking(piece)
                if legacy or shown_text:
                    yield ReasoningDelta(text=shown_text, legacy=legacy)
                await asyncio.sleep(0.01)
            closing = think.close()
            if closing:
                yield ReasoningEnd(legacy=closing)

        if "tool" in words:
            args = {"name": "scripted"}
            _request_ctx.record_tool_call("read_knowledge", args)
            yield ToolCall(id="call_tool", name="read_knowledge", args=args,
                           legacy=tool_events.format_call("read_knowledge", args, mode=self._tool_mode))
            await asyncio.sleep(0.01)
            yield ToolResult(id="call_tool", name="read_knowledge", ok=True)

        if "fail" in words:
            args = {"pattern": "missing"}
            _request_ctx.record_tool_call("grep_data", args)
            yield ToolCall(id="call_fail", name="grep_data", args=args,
                           legacy=tool_events.format_call("grep_data", args, mode=self._tool_mode))
            await asyncio.sleep(0.01)
            yield ToolResult(id="call_fail", name="grep_data", ok=False,
                             message="The tool did not complete. The agent may retry or ask for "
                                     "more information.",
                             legacy=tool_events.format_error("grep_data"))

        if "artifact" in words:
            args = {"filename": ARTIFACT_NAME}
            _request_ctx.record_tool_call("write_artifact", args)
            yield ToolCall(id="call_artifact", name="write_artifact", args=args,
                           legacy=tool_events.format_call("write_artifact", args, mode=self._tool_mode))
            self._write_artifact(request)
            yield ToolResult(id="call_artifact", name="write_artifact", ok=True)

        if "error" in words:
            for piece in _chunks("Starting on that. "):
                yield text(piece)
            self.last_error = RuntimeError("scripted failure")
            _request_ctx.note_usage(input_tokens=len(prompt.split()), output_tokens=output_words,
                                    model=self.model_id, cost_usd=0.0, status="error")
            detail = f"{type(self.last_error).__name__}: {self.last_error}"
            yield Notice(kind="error", text=detail, legacy=f"\n\n[agent error: {detail}]")
            return

        if "slow" in words:
            total = _slow_seconds()
            for n in range(1, 41):
                yield text(f"Chunk {n}. ")
                await asyncio.sleep(total / 40)
        else:
            body = _MARKDOWN_REPLY if "markdown" in words else self._default_reply(request)
            for piece in _chunks(body):
                yield text(piece)
                await asyncio.sleep(0)

        footer = tool_events.format_artifact_footer(_request_ctx.drain_artifacts(), "".join(shown))
        if footer:
            yield Notice(kind="artifacts", text=footer, legacy=footer)
        _request_ctx.note_usage(input_tokens=len(prompt.split()), output_tokens=output_words,
                                model=self.model_id, cost_usd=0.0, status="ok")

    def _default_reply(self, request: str) -> str:
        quote = _quote(request) or "(an empty message)"
        reply = f"You said: “{quote}”\n\nThis is a **scripted** reply from the Hubzoid test runtime."
        files = _NOTE.findall(request)
        if files:
            reply += "\n\nAttached: " + ", ".join(f"`{name.strip()}`" for name in files) + "."
        return reply

    def _write_artifact(self, request: str) -> None:
        """Save a small file the way ``write_artifact`` does: in this chat's
        artifact folder, with its signed link registered for the footer."""
        from . import memory as memlib
        from .tools.files import _artifact_url

        chat_id = _request_ctx.get_chat_id()
        if not chat_id:
            return
        target = memlib.chat_artifact_dir(self.hub_dir, chat_id) / ARTIFACT_NAME
        target.write_text(f"# Scripted report\n\nRequest: {_quote(request)}\n", encoding="utf-8")
        url = _artifact_url(ARTIFACT_NAME, self.hub_dir)
        if url:
            _request_ctx.record_artifact(ARTIFACT_NAME, url)


def _slow_seconds() -> float:
    try:
        return max(0.0, float(os.environ.get("HUBZOID_TEST_SLOW_SECONDS", "20")))
    except ValueError:
        return 20.0


def complete(spec: dict, *, model_id: str) -> tuple[str, dict]:
    """The scripted answer to one tool-free call (``runtime.complete_once``)."""
    require_enabled(model_id)
    prompt = str(spec.get("prompt") or "")
    if spec.get("response_format") == "json":
        text = json.dumps({"answer": "scripted"})
    else:
        body = prompt.split("Message:", 1)[-1]
        words = _WORD.findall(body)[:4]
        text = ("About " + " ".join(w.lower() for w in words)) if words else "Scripted conversation"
    usage = {"input_tokens": len(prompt.split()), "output_tokens": len(text.split()),
             "cost_usd": 0.0, "model": model_id}
    return text, usage
