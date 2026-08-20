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

    async def aopen(self) -> None: ...

    async def aclose(self) -> None: ...


def _resolve_model_id(hub_dir: Path, settings) -> str:
    """The effective MODEL for a hub.

    Precedence: ``.env`` MODEL -> AGENTS.md ``model:`` frontmatter -> the
    bundled Claude Agent SDK backend (``claude-local``). The final default
    means a hub with no model configured anywhere still runs (Claude SDK on
    Sonnet via the bundled ``claude`` login) instead of erroring at build
    time — operators don't have to set MODEL just to get started.
    """
    model_id = (settings.model or "").strip()
    if model_id:
        return model_id
    try:
        from .loaders import agents as agents_loader
        model_id = (agents_loader.load_main(hub_dir).spec.model or "").strip()
    except Exception:  # noqa: BLE001 — missing/invalid AGENTS.md falls through to default
        model_id = ""
    return model_id or "claude-local"


_CLAUDE_TIERS = frozenset({"opus", "sonnet", "haiku"})


def _apply_model_override(base_id: str, override: str) -> tuple[str, str]:
    """Resolve a per-run model override, PRESERVING the hub's backend.

    Backend selection keys off the returned model id, so a bare Claude tier
    (``opus``/``sonnet``/``haiku``) or a ``claude-*`` id on a claude-local hub
    is pinned *within* the claude-local backend (``claude-local/<x>``) — a
    task's ``model: opus`` must not accidentally route to the OpenAI backend
    (and ``_parse_model_pin`` needs the ``claude-local/`` prefix to extract the
    tier). A full id that already names its backend (``claude-local/...``, or an
    OpenAI/LiteLLM id like ``gpt-4o``) is used verbatim, so a deliberate
    cross-backend switch still works.

    Returns (model_id_for_backend_selection, override_passed_to_factory) — the
    two are the same string; both are returned for call-site clarity.
    """
    ov = override.strip()
    low = ov.lower()
    if base_id.lower().startswith("claude-local") and not low.startswith("claude-local"):
        if low in _CLAUDE_TIERS or low.startswith("claude-"):
            # Use the lowercased form — the Claude CLI recognizes `opus`/`sonnet`/
            # `haiku` and `claude-*` ids only in lowercase, and _parse_model_pin
            # passes the suffix through verbatim.
            norm = f"claude-local/{low}"
            return norm, norm
    return ov, ov


def build(hub_dir: Path, *, extra_tools: dict | None = None,
          max_turns: int | None = None, model: str | None = None) -> Runtime:
    """Pick the backend for this hub based on `MODEL` in <hub>/.env.

    `MODEL=claude-local` -> Claude Agent SDK (subprocess + `claude` login).
    anything else        -> OpenAI Agents SDK + LiteLLM (existing behavior).
    `MODEL` unset (and no AGENTS.md `model:`) -> claude-local default; see
    `_resolve_model_id`. We never error on a missing model.

    `extra_tools` ({name: FunctionTool}) are merged into the registry on top
    of built-ins + hub-local — used by scheduled-task runs to inject their
    internal tools (run_git, write_hub_file) without leaking them into chat.
    `max_turns` overrides the per-call agent-turn cap (default 20) — long
    unattended runs need more headroom than a chat turn.
    `model` overrides the hub's configured model for this build only — used by
    a scheduled task's `model:` frontmatter so one job can run on a heavier
    (or cheaper) tier than the hub default, without touching .env. It wins
    over both `.env` MODEL and AGENTS.md `model:`.
    """
    hub_dir = Path(hub_dir).resolve()
    settings = settingslib.load(hub_dir)
    base_id = _resolve_model_id(hub_dir, settings)
    override = (model or "").strip() or None
    if override:
        model_id, override = _apply_model_override(base_id, override)
    else:
        model_id = base_id
        if not (settings.model or "").strip():
            log.info(
                "hub %s: no MODEL in .env; defaulting to %s",
                hub_dir.name, model_id,
            )

    if model_id.lower().startswith("claude-local"):
        from .factory_claude import build_claude_runtime
        return build_claude_runtime(hub_dir, extra_tools=extra_tools,
                                    max_turns=max_turns, model_override=override)

    from . import otel as otellib
    otellib.openai_otel_setup(endpoint=settings.otel_endpoint, hub=hub_dir.name)
    from .factory import build_agent
    return OpenAIAgentsRuntime(
        build_agent(hub_dir, extra_tools=extra_tools, model_override=override),
        max_turns=max_turns, tool_mode=settings.show_tools, hub_dir=hub_dir,
        vision=(settings.vision_enabled, settings.vision_max_edge,
                settings.vision_max_images))


# ---------------------------------------------------------------------------
# OpenAI Agents SDK backend (the default, factored out of server.py).
# ---------------------------------------------------------------------------
class OpenAIAgentsRuntime:
    """Wraps an `agents.Agent` + `Runner.run_streamed` behind the Runtime API."""

    def __init__(self, agent, *, max_turns: int | None = None,
                 tool_mode: str = "compact", hub_dir=None,
                 vision: tuple[bool, int, int] = (True, 1568, 4)):
        self._agent = agent
        self._max_turns = max_turns or 20
        self._tool_mode = tool_mode
        self._hub_dir = hub_dir
        self._vision = vision
        self.name = agent.name
        # MCP servers come back from the loader unconnected. The Agents SDK
        # requires they be connected before it will list their tools (the
        # Claude backend manages this itself, hence this is OpenAI-only). The
        # connection MUST be opened and closed in the SAME asyncio task — an
        # MCP stdio server binds an anyio task group / cancel scope to the
        # opening task, so connecting inside a per-request stream() and tearing
        # down elsewhere raises "cancel scope in a different task" /
        # ClosedResourceError. So callers open once in a stable task (the
        # bridge lifespan, or a one-shot CLI coroutine) via aopen()/aclose();
        # request tasks then just *use* the already-connected servers.
        self._mcp_servers = list(getattr(agent, "mcp_servers", []) or [])
        self._stack = None
        self._opened = False

    async def aopen(self) -> None:
        """Connect MCP servers within the calling task. A server that fails to
        connect is dropped (with a warning) rather than crashing the agent —
        a broken connector shouldn't take down chat or a scheduled run.

        Idempotent. Pair every aopen() with an aclose() in the SAME task."""
        if self._opened:
            return
        self._opened = True
        if not self._mcp_servers:
            self._agent.mcp_servers = []
            return
        from contextlib import AsyncExitStack

        self._stack = AsyncExitStack()
        live = []
        for s in self._mcp_servers:
            name = getattr(s, "name", "?")
            try:
                await self._stack.enter_async_context(s)  # __aenter__ = connect
                live.append(s)
                log.info("MCP server %r connected", name)
            except Exception as exc:  # noqa: BLE001
                log.warning("MCP server %r failed to connect; disabling it: %s",
                            name, exc)
        # Keep only servers that actually connected, so the SDK never tries to
        # list_tools() on a dead one ("Server not initialized" error).
        self._agent.mcp_servers = live

    async def aclose(self) -> None:
        """Tear down MCP connections. Must run in the same task as aopen()."""
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except Exception as exc:  # noqa: BLE001 — teardown best-effort
                log.warning("MCP cleanup error (ignored): %s", exc)
            self._stack = None
        self._opened = False

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        from agents import ItemHelpers, Runner
        from openai.types.responses import ResponseTextDeltaEvent

        from . import _request_ctx, tool_events

        text_accumulated = False
        shown: list[str] = []
        # Native image vision: expand any [Image: name] reference into an input
        # list (text + input_image blocks). Plain string when nothing to inject.
        run_input = prompt
        if self._hub_dir is not None:
            from . import vision_inject
            enabled, max_edge, max_images = self._vision
            run_input = vision_inject.openai_input(
                prompt, self._hub_dir, _request_ctx.get_chat_id(),
                enabled=enabled, max_edge=max_edge, max_images=max_images,
            )
        try:
            result = Runner.run_streamed(self._agent, run_input, max_turns=self._max_turns)
            async for event in result.stream_events():
                if event.type == "raw_response_event" and isinstance(event.data, ResponseTextDeltaEvent):
                    if event.data.delta:
                        text_accumulated = True
                        shown.append(event.data.delta)
                        yield event.data.delta
                    continue
                if event.type == "run_item_stream_event":
                    item = event.item
                    if item.type == "message_output_item" and not text_accumulated:
                        text = ItemHelpers.text_message_output(item)
                        if text:
                            shown.append(text)
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
                        # Record before formatting: `format_call` returns ""
                        # when SHOW_TOOLS=off, but an eval's expect_tools must
                        # still see the call.
                        _request_ctx.record_tool_call(
                            tool_events.short_name(name), args)
                        line = tool_events.format_call(
                            tool_events.short_name(name), args,
                            mode=self._tool_mode,
                        )
                        if line:
                            yield line
            # Surface final token usage for the usage envelope (best-effort).
            _record_openai_usage(result)
            # Surface any download link the model did not echo itself.
            footer = tool_events.format_artifact_footer(
                _request_ctx.drain_artifacts(), "".join(shown))
            if footer:
                yield footer
        except Exception as exc:  # noqa: BLE001
            log.exception("openai-agents stream failed")
            yield f"\n\n[agent error: {type(exc).__name__}: {exc}]"

    async def run(self, prompt: str) -> str:
        pieces: list[str] = []
        async for chunk in self.stream(prompt):
            pieces.append(chunk)
        return "".join(pieces)


def _record_openai_usage(result) -> None:
    """Surface the OpenAI Agents run's token usage for the usage envelope.

    `result.context_wrapper.usage` accumulates across the run. The OpenAI path
    reports no dollar cost, so `cost_usd` is None (token counts still give
    relative cost). Best-effort — never break the stream on a shape change.
    """
    try:
        from . import _request_ctx
        usage = getattr(getattr(result, "context_wrapper", None), "usage", None)
        if usage is None:
            return
        inp = int(getattr(usage, "input_tokens", 0) or 0)
        out = int(getattr(usage, "output_tokens", 0) or 0)
        _request_ctx.record_usage({
            "input_tokens": inp,
            "output_tokens": out,
            "total_tokens": int(getattr(usage, "total_tokens", inp + out) or (inp + out)),
            "cost_usd": None,
            "num_turns": getattr(usage, "requests", None),
        })
    except Exception as exc:  # noqa: BLE001 — telemetry must never break chat
        log.debug("openai usage capture skipped: %s", exc)


# Convenience for callers that want a JSON-debuggable view of which backend
# a hub resolved to (used by `hubzoid doctor`).
def describe(hub_dir: Path) -> str:
    settings = settingslib.load(hub_dir)
    model = _resolve_model_id(hub_dir, settings)
    backend = "claude-local" if model.lower().startswith("claude-local") else "openai-agents"
    return json.dumps({"backend": backend, "model": model})
