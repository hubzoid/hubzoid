"""Claude Agent SDK backend — same hub folder, same loaders, same tools.

When `MODEL=claude-local` is set, `runtime.build(hub_dir)` returns a
`ClaudeRuntime` from this file instead of an `OpenAIAgentsRuntime`. The
point of this module is to keep the user-facing surface (tool names,
schemas, outputs, skills, knowledge) identical between backends — the
only genuine difference is which LLM is deciding.

How identity is preserved:
  * Skills, knowledge, MCP configs    -> same loaders, unchanged.
  * `agents/` folder                  -> promoted to skills by the shared
    `_load_skills_and_promoted_agents` helper; no Claude SDK sub-agents
    are wired. The main agent loads them inline via `load_skill(<name>)`.
  * Tools (pre-shipped + hub-local)   -> same FunctionTool registry from
    `make_builtin_tools(ctx)` + `tools_local_loader.load_all`. Each
    FunctionTool is wrapped via `_to_claude_tool` and bundled into a
    single in-process MCP server named "hubzoid". The model sees them as
    `mcp__hubzoid__<name>`.

Auth: the Claude Agent SDK shells out to the local `claude` CLI
subprocess, which authenticates via `claude login` (subscription) or
`ANTHROPIC_API_KEY`, in that order. No hubzoid-managed key.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, AsyncIterator

from . import memory as memlib
from . import settings as settingslib
from . import tool_events
from .factory import HubContext, _compose_instructions, _load_skills_and_promoted_agents
from .loaders import agents as agents_loader
from .loaders import knowledge as knowledge_loader
from .loaders import mcp as mcp_loader
from .loaders import tools_local as tools_local_loader
from .tools import make_all as make_builtin_tools

log = logging.getLogger("hubzoid.claude")

_MCP_NAMESPACE = "hubzoid"


# ---------------------------------------------------------------------------
# Tool adapter: openai-agents FunctionTool -> claude-agent-sdk @tool
# ---------------------------------------------------------------------------
def _to_claude_tool(ft):
    """Wrap a single FunctionTool as a Claude SDK in-process tool.

    The FunctionTool exposes four runtime-neutral fields:
      - name                 -> Claude tool name
      - description          -> Claude tool description
      - params_json_schema   -> Claude tool input schema (raw JSON Schema)
      - on_invoke_tool(ctx, json_args_str) -> the actual work

    Hubzoid's tools close over their HubContext at construction time and do
    not consult the run-context at call time. BUT the openai-agents
    FunctionTool dispatcher and error-reporter chain reach for
    ``ctx.run_config.trace_include_sensitive_data`` even on the happy path
    in some code paths. Passing ``None`` crashes inner errors with
    ``'NoneType' object has no attribute 'run_config'``.

    We construct a minimal valid ToolContext per call: real tool_name,
    a generated tool_call_id, the raw args JSON, and ``run_config=None``
    (which the dispatcher handles gracefully — only a missing attribute
    is the problem).
    """
    from agents import RunConfig
    from agents.tool_context import ToolContext
    from claude_agent_sdk import tool
    import uuid

    name = ft.name
    description = ft.description or f"Tool: {name}"
    schema = ft.params_json_schema or {"type": "object", "properties": {}}

    @tool(name, description, schema)
    async def _wrapper(args: dict[str, Any]) -> dict[str, Any]:
        args_json = json.dumps(args or {})
        ctx = ToolContext(
            context=None,
            tool_name=name,
            tool_call_id=f"hubzoid-{uuid.uuid4().hex[:12]}",
            tool_arguments=args_json,
            run_config=RunConfig(),
        )
        try:
            result = await ft.on_invoke_tool(ctx, args_json)
        except Exception as exc:  # noqa: BLE001
            log.exception("tool %s failed", name)
            text = f"[tool {name} error: {type(exc).__name__}: {exc}]"
        else:
            text = result if isinstance(result, str) else json.dumps(result, default=str)
        return {"content": [{"type": "text", "text": text}]}

    return _wrapper


def _build_mcp_server(registry: dict):
    """Bundle the FunctionTool registry into a single in-process MCP server."""
    from claude_agent_sdk import create_sdk_mcp_server

    tools = [_to_claude_tool(ft) for ft in registry.values()]
    return create_sdk_mcp_server(name=_MCP_NAMESPACE, version="0.1.0", tools=tools)


def _allowed_tool_names(registry: dict, mcp_specs: dict[str, dict]) -> list[str]:
    """Build the `allowed_tools` whitelist Claude sees.

    Includes:
      - mcp__hubzoid__<name> for every wrapped FunctionTool
      - mcp__<server>__* for every external MCP server (let the SDK expand)

    No 'Agent' entry. Hubzoid no longer wires Claude SDK sub-agents — the
    `agents/` folder is promoted to skills, which the main agent loads
    inline via `load_skill`.
    """
    out = [f"mcp__{_MCP_NAMESPACE}__{name}" for name in registry]
    for server in mcp_specs:
        out.append(f"mcp__{server}__*")
    return out


# ---------------------------------------------------------------------------
# Public entry: build a ClaudeRuntime for the hub.
# ---------------------------------------------------------------------------
def build_claude_runtime(hub_dir: Path, *, extra_tools: dict | None = None,
                         max_turns: int | None = None) -> "ClaudeRuntime":
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "MODEL=claude-local requires the claude-agent-sdk package, which is\n"
            "bundled with hubzoid. Reinstall to repair:\n"
            "    pip install --force-reinstall hubzoid"
        ) from exc

    hub_dir = Path(hub_dir).resolve()
    if not hub_dir.is_dir():
        raise FileNotFoundError(f"hub directory not found: {hub_dir}")

    settings = settingslib.load(hub_dir)
    session_id = memlib.make_session_id()
    output_dir = memlib.session_output_dir(hub_dir, session_id)

    skills = _load_skills_and_promoted_agents(hub_dir)
    knowledge = knowledge_loader.load_all(hub_dir)
    log.info(
        "hub %s (claude-local): %d skill(s), %d knowledge doc(s)",
        hub_dir.name, len(skills), len(knowledge),
    )

    ctx = HubContext(
        hub_dir=hub_dir,
        output_dir=output_dir,
        session_id=session_id,
        settings=settings,
        skills=skills,
        knowledge=knowledge,
    )

    # Same registry as the OpenAI path. Built-ins + hub-local; local shadows
    # built-ins on name conflicts; caller-injected extras (scheduled-task
    # internals) win over both.
    builtin = make_builtin_tools(ctx)
    local = tools_local_loader.load_all(hub_dir)
    overlap = set(builtin) & set(local)
    if overlap:
        log.info("hub-local tools override built-ins: %s", sorted(overlap))
    registry: dict = {**builtin, **local, **(extra_tools or {})}

    # MCP: external servers from the hub's connectors/.mcp.json (raw dicts;
    # Claude SDK accepts the same JSON shape) plus our in-process hubzoid
    # server that exposes the FunctionTool registry.
    external_mcp = mcp_loader.load_all_raw(hub_dir)
    hubzoid_mcp = _build_mcp_server(registry)
    mcp_servers = {**external_mcp, _MCP_NAMESPACE: hubzoid_mcp}

    main_spec = agents_loader.load_main(hub_dir)
    main_name = main_spec.spec.name
    main_instructions = _compose_instructions(main_spec.instructions, ctx, backend="claude-local")

    allowed = _allowed_tool_names(registry, mcp_specs=external_mcp)
    model_pin = _parse_model_pin(settings.model)

    from claude_agent_sdk import ClaudeAgentOptions

    # We deliberately do NOT pass setting_sources — hubzoid is the source of
    # truth for what a hub means. Filesystem auto-discovery from .claude/
    # is disabled to keep parity with the OpenAI backend.
    #
    # `tools=[]` disables every SDK built-in (Bash, Read, Edit, Write, Task,
    # WebFetch, Grep, Glob, ...). Without this, the SDK falls back to its
    # `claude_code` preset and the agent has the FULL Claude Code tool
    # surface, which (a) breaks parity with the OpenAI backend that has
    # none of those, and (b) is the bug behind the rabbit hole where the
    # agent escapes `read_upload` and tries to Bash/Read uploaded files
    # on hallucinated paths under ~/.claude/projects/. `allowed_tools`
    # is NOT a restriction list — it only controls permission prompting;
    # `tools` is the gate. See test_factory_claude_tool_gating.py.
    opts_kwargs: dict[str, Any] = dict(
        system_prompt=main_instructions,
        tools=[],
        allowed_tools=allowed,
        mcp_servers=mcp_servers,
        setting_sources=[],  # explicit: no Claude Code config discovery
        include_partial_messages=True,  # token-level deltas via StreamEvent
    )
    if model_pin is not None:
        opts_kwargs["model"] = model_pin
    if max_turns is not None:
        opts_kwargs["max_turns"] = max_turns
    try:
        options = ClaudeAgentOptions(**opts_kwargs)
    except TypeError:
        # Older claude-agent-sdk without max_turns — drop it rather than die.
        opts_kwargs.pop("max_turns", None)
        options = ClaudeAgentOptions(**opts_kwargs)

    return ClaudeRuntime(name=main_name, options=options)


_CLAUDE_LOCAL_DEFAULT = "sonnet"


def _parse_model_pin(model_setting: str | None) -> str | None:
    """Extract the model suffix from `MODEL=claude-local[/<pin>]`.

    Bare `claude-local` (no suffix) defaults to **Sonnet 4.x**. We
    originally defaulted to Haiku for low TTFT, but Haiku tends to ask
    the user to choose between options instead of executing documented
    workflows — reproducibly broke the IRS-hub QA pipeline that the
    prs-agent Claude Code session handled cleanly on Sonnet. Sonnet's
    decisiveness on routing rules matters more than Haiku's latency for
    agentic hubs. Operators who specifically want Haiku speed opt in
    explicitly via `claude-local/haiku`.

    Returns None when MODEL isn't set or isn't a claude-local variant.
    Examples:
      claude-local                -> "sonnet"            (default)
      claude-local/sonnet         -> "sonnet"            (explicit, same effect)
      claude-local/haiku          -> "haiku"             (opt in for low TTFT)
      claude-local/opus           -> "opus"
      claude-local/claude-opus-4-7 -> "claude-opus-4-7"  (full ids pass through)
    """
    if not model_setting:
        return None
    stripped = model_setting.strip()
    if stripped == "claude-local":
        return _CLAUDE_LOCAL_DEFAULT
    if "/" not in stripped:
        return None
    suffix = stripped.split("/", 1)[1].strip()
    return suffix or None


# ---------------------------------------------------------------------------
# Runtime implementation.
# ---------------------------------------------------------------------------
class ClaudeRuntime:
    """Thin async-iterator adapter around `claude_agent_sdk.query(...)`."""

    def __init__(self, *, name: str, options):
        self.name = name
        self._options = options

    async def aopen(self) -> None:
        """No-op: the Claude Agent SDK manages MCP lifecycle internally.
        Present so callers can drive both backends uniformly."""

    async def aclose(self) -> None:
        """No-op counterpart to aopen() — see above."""

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        """Yield text deltas + inline tool-activity markers as they arrive.

        Three event streams are interleaved into one text stream:

        1. **Assistant text**: token-level deltas from `StreamEvent`.
        2. **Tool calls**: a single blockquote line per `ToolUseBlock`,
           emitted at call start. No matching "returned" line.
        3. **Tool errors**: a ⚠ blockquote line for any `ToolResultBlock`
           that arrives with `is_error=True`.

        Fallback: if partial events don't arrive (older SDK?), we surface
        the final `ResultMessage.result` so the user still sees the reply.
        """
        from claude_agent_sdk import AssistantMessage, ResultMessage, UserMessage, query
        from claude_agent_sdk.types import StreamEvent, TextBlock, ToolResultBlock, ToolUseBlock

        streamed_any = False
        final_result: str | None = None
        # tool_use_id -> short name. Used only to identify error result blocks
        # so we can surface them with a ⚠ marker. Successful results emit
        # nothing — the call line was already shown.
        tool_use_names: dict[str, str] = {}
        try:
            async for message in query(prompt=prompt, options=self._options):
                # --- Token-level text deltas ---
                if isinstance(message, StreamEvent):
                    event = getattr(message, "event", None) or {}
                    if event.get("type") == "content_block_delta":
                        delta = event.get("delta") or {}
                        if delta.get("type") == "text_delta":
                            text = delta.get("text") or ""
                            if text:
                                streamed_any = True
                                yield text
                    continue

                # --- Tool calls announced as full assistant message blocks ---
                if isinstance(message, AssistantMessage):
                    for block in getattr(message, "content", []) or []:
                        if isinstance(block, ToolUseBlock):
                            tid = getattr(block, "id", None) or ""
                            if tid in tool_use_names:
                                continue
                            short = tool_events.short_name(block.name)
                            tool_use_names[tid] = short
                            yield tool_events.format_call(
                                short, getattr(block, "input", None),
                            )
                    continue

                # --- Tool results: emit a line only on error. Success is
                #     implicit (the call line was already shown).
                if isinstance(message, UserMessage):
                    for block in getattr(message, "content", []) or []:
                        if isinstance(block, ToolResultBlock):
                            if not bool(getattr(block, "is_error", False)):
                                continue
                            tid = getattr(block, "tool_use_id", "") or ""
                            tool_name = tool_use_names.get(tid, "tool")
                            yield tool_events.format_error(tool_name)
                    continue

                # --- Final aggregate (fallback if partials are missing) ---
                if isinstance(message, ResultMessage):
                    final_result = getattr(message, "result", None)
        except Exception as exc:  # noqa: BLE001
            log.exception("claude stream failed")
            yield f"\n\n[agent error: {type(exc).__name__}: {exc}]"
            return

        if not streamed_any and final_result:
            yield final_result

    async def run(self, prompt: str) -> str:
        pieces: list[str] = []
        async for chunk in self.stream(prompt):
            pieces.append(chunk)
        return "".join(pieces)


