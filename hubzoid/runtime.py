"""Runtime abstraction — one hub folder, swappable execution backend.

A `Runtime` exposes a tiny surface that the FastAPI bridge (`server.py`) and
CLI (`cli.py`) consume without caring which engine sits underneath:

  * `name`               -> what /v1/models reports
  * `stream(prompt)`     -> async iterator of text deltas (SSE-friendly)
  * `run(prompt)`        -> single accumulated response string

Two backends today:
  * OpenAI Agents SDK (default) — `OpenAIAgentsRuntime`, in this file.
  * Claude Agent SDK (`MODEL=claude-local`) — `ClaudeRuntime`, in
    `factory_claude.py`.

Loaders and tool implementations are runtime-neutral by contract (see
AGENTS.md). Only this module and the two factory files know which engine is
in play. Keep it that way.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import AsyncIterator, Protocol

from . import settings as settingslib

log = logging.getLogger("hubzoid.runtime")


class Runtime(Protocol):
    name: str

    def stream(self, prompt: str) -> AsyncIterator[str]: ...

    async def run(self, prompt: str) -> str: ...


def build(hub_dir: Path) -> Runtime:
    """Pick the backend for this hub based on `MODEL` in <hub>/.env.

    `MODEL=claude-local` -> Claude Agent SDK (subprocess + `claude` login).
    anything else        -> OpenAI Agents SDK + LiteLLM (existing behavior).
    """
    hub_dir = Path(hub_dir).resolve()
    settings = settingslib.load(hub_dir)
    model_id = (settings.model or "").strip().lower()

    if model_id.startswith("claude-local"):
        from .factory_claude import build_claude_runtime
        return build_claude_runtime(hub_dir)

    from .factory import build_agent
    return OpenAIAgentsRuntime(build_agent(hub_dir))


# ---------------------------------------------------------------------------
# OpenAI Agents SDK backend (the default, factored out of server.py).
# ---------------------------------------------------------------------------
class OpenAIAgentsRuntime:
    """Wraps an `agents.Agent` + `Runner.run_streamed` behind the Runtime API."""

    def __init__(self, agent):
        self._agent = agent
        self.name = agent.name

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        from agents import ItemHelpers, Runner
        from openai.types.responses import ResponseTextDeltaEvent

        from . import tool_events

        text_accumulated = False
        try:
            result = Runner.run_streamed(self._agent, prompt, max_turns=20)
            async for event in result.stream_events():
                if event.type == "raw_response_event" and isinstance(event.data, ResponseTextDeltaEvent):
                    if event.data.delta:
                        text_accumulated = True
                        yield event.data.delta
                    continue
                if event.type == "run_item_stream_event":
                    item = event.item
                    if item.type == "message_output_item" and not text_accumulated:
                        text = ItemHelpers.text_message_output(item)
                        if text:
                            yield text
                    elif item.type == "tool_call_item":
                        # One line per tool call. No matching "returned" line.
                        raw = getattr(item, "raw_item", None)
                        name = getattr(raw, "name", None) or "tool"
                        args = getattr(raw, "arguments", None)
                        if isinstance(args, str) and args:
                            try:
                                import json as _json
                                args = _json.loads(args)
                            except Exception:  # noqa: BLE001
                                pass
                        yield tool_events.format_call(
                            tool_events.short_name(name), args,
                        )
        except Exception as exc:  # noqa: BLE001
            log.exception("openai-agents stream failed")
            yield f"\n\n[agent error: {type(exc).__name__}: {exc}]"

    async def run(self, prompt: str) -> str:
        pieces: list[str] = []
        async for chunk in self.stream(prompt):
            pieces.append(chunk)
        return "".join(pieces)


# Convenience for callers that want a JSON-debuggable view of which backend
# a hub resolved to (used by `hubzoid doctor`).
def describe(hub_dir: Path) -> str:
    settings = settingslib.load(hub_dir)
    model = (settings.model or "").strip()
    if model.lower().startswith("claude-local"):
        return json.dumps({"backend": "claude-local", "model": model})
    return json.dumps({"backend": "openai-agents", "model": model or "(unset)"})
