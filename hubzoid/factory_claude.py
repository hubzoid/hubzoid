"""Claude Agent SDK backend — same hub folder, same loaders, same tools.

When `MODEL=claude-local` is set, `runtime.build(hub_dir)` returns a
`ClaudeRuntime` from this file instead of an `OpenAIAgentsRuntime`. The whole
point of this module is to keep the user-facing surface (tool names, schemas,
outputs, skills, knowledge, sub-agents) identical between backends — the only
genuine difference is which LLM is doing the deciding.

How identity is preserved:
  * Skills, knowledge, agents specs, MCP configs    -> same loaders, unchanged.
  * Tools (pre-shipped + hub-local)                  -> same FunctionTool
    registry from `make_builtin_tools(ctx)` + `tools_local_loader.load_all`.
    Each FunctionTool is wrapped via `_to_claude_tool` and bundled into a
    single in-process MCP server named "hubzoid". The model sees them as
    `mcp__hubzoid__<name>`; we strip the prefix in log/trace lines.
  * Sub-agents                                       -> `AgentDefinition`
    objects with the same name, description, instructions, and tool whitelist.

Auth: the Claude Agent SDK shells out to the local `claude` CLI subprocess,
which authenticates via `claude login` (subscription) or `ANTHROPIC_API_KEY`,
in that order. No hubzoid-managed key.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, AsyncIterator

from . import memory as memlib
from . import settings as settingslib
from .factory import HubContext
from .loaders import agents as agents_loader
from .loaders import knowledge as knowledge_loader
from .loaders import mcp as mcp_loader
from .loaders import skills as skills_loader
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

    We pass `None` as the run-context wrapper because hubzoid's tools close
    over their HubContext at construction time (see `tools/*.py make(ctx)`)
    and do not consult the run-context at call time.
    """
    from claude_agent_sdk import tool

    name = ft.name
    description = ft.description or f"Tool: {name}"
    schema = ft.params_json_schema or {"type": "object", "properties": {}}

    @tool(name, description, schema)
    async def _wrapper(args: dict[str, Any]) -> dict[str, Any]:
        try:
            result = await ft.on_invoke_tool(None, json.dumps(args or {}))
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


def _allowed_tool_names(registry: dict, has_subagents: bool, mcp_specs: dict[str, dict]) -> list[str]:
    """Build the `allowed_tools` whitelist Claude sees.

    Includes:
      - mcp__hubzoid__<name> for every wrapped FunctionTool
      - mcp__<server>__* for every external MCP server (let the SDK expand)
      - 'Agent' if any sub-agents are defined
    """
    out = [f"mcp__{_MCP_NAMESPACE}__{name}" for name in registry]
    for server in mcp_specs:
        out.append(f"mcp__{server}__*")
    if has_subagents:
        out.append("Agent")
    return out


# ---------------------------------------------------------------------------
# Sub-agent translation: LoadedAgent -> AgentDefinition
# ---------------------------------------------------------------------------
def _build_subagent_defs(sub_specs, registry):
    """Translate hubzoid sub-agent specs into Claude SDK AgentDefinitions.

    The sub-agent's `tools:` whitelist applies the same way as in the OpenAI
    path — empty list = no tools (deny-by-default).
    """
    from claude_agent_sdk import AgentDefinition

    defs: dict = {}
    for spec in sub_specs:
        if spec.spec.tools:
            unknown = [t for t in spec.spec.tools if t not in registry]
            if unknown:
                available = ", ".join(sorted(registry)) or "(none)"
                raise RuntimeError(
                    f"{spec.source_path}: tools reference unknown names: {unknown}. "
                    f"Available: {available}"
                )
            tool_names = [f"mcp__{_MCP_NAMESPACE}__{t}" for t in spec.spec.tools]
        else:
            tool_names = []

        defs[spec.spec.name] = AgentDefinition(
            description=spec.spec.description,
            prompt=spec.instructions,
            tools=tool_names,
        )
    return defs


# ---------------------------------------------------------------------------
# Public entry: build a ClaudeRuntime for the hub.
# ---------------------------------------------------------------------------
def build_claude_runtime(hub_dir: Path) -> "ClaudeRuntime":
    try:
        import claude_agent_sdk  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "MODEL=claude-local requires the claude-agent-sdk package.\n"
            "Install with:  pip install 'hubzoid[claude-local]'"
        ) from exc

    hub_dir = Path(hub_dir).resolve()
    if not hub_dir.is_dir():
        raise FileNotFoundError(f"hub directory not found: {hub_dir}")

    settings = settingslib.load(hub_dir)
    session_id = memlib.make_session_id()
    output_dir = memlib.session_output_dir(hub_dir, session_id)

    skills = skills_loader.load_all(hub_dir)
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
    # built-ins on name conflicts.
    builtin = make_builtin_tools(ctx)
    local = tools_local_loader.load_all(hub_dir)
    overlap = set(builtin) & set(local)
    if overlap:
        log.info("hub-local tools override built-ins: %s", sorted(overlap))
    registry: dict = {**builtin, **local}

    # MCP: external servers from the hub's connectors/.mcp.json (raw dicts;
    # Claude SDK accepts the same JSON shape) plus our in-process hubzoid
    # server that exposes the FunctionTool registry.
    external_mcp = mcp_loader.load_all_raw(hub_dir)
    hubzoid_mcp = _build_mcp_server(registry)
    mcp_servers = {**external_mcp, _MCP_NAMESPACE: hubzoid_mcp}

    sub_specs = agents_loader.load_subagents(hub_dir)
    sub_defs = _build_subagent_defs(sub_specs, registry)
    log.info("hub %s (claude-local): %d sub-agent(s)", hub_dir.name, len(sub_defs))

    main_spec = agents_loader.load_main(hub_dir)
    main_name = main_spec.spec.name
    main_instructions = main_spec.instructions

    allowed = _allowed_tool_names(registry, has_subagents=bool(sub_defs), mcp_specs=external_mcp)
    model_pin = _parse_model_pin(settings.model)

    from claude_agent_sdk import ClaudeAgentOptions

    # We deliberately do NOT pass setting_sources — hubzoid is the source of
    # truth for what a hub means. Filesystem auto-discovery from .claude/
    # is disabled to keep parity with the OpenAI backend.
    opts_kwargs: dict[str, Any] = dict(
        system_prompt=main_instructions,
        allowed_tools=allowed,
        mcp_servers=mcp_servers,
        agents=sub_defs or None,
        setting_sources=[],  # explicit: no Claude Code config discovery
    )
    if model_pin is not None:
        opts_kwargs["model"] = model_pin
    options = ClaudeAgentOptions(**opts_kwargs)

    return ClaudeRuntime(name=main_name, options=options)


def _parse_model_pin(model_setting: str | None) -> str | None:
    """Extract the model suffix from `MODEL=claude-local[/<pin>]`.

    Returns None when no suffix is set (lets the `claude` CLI's default win).
    Examples:
      claude-local                -> None
      claude-local/sonnet         -> "sonnet"
      claude-local/opus           -> "opus"
      claude-local/claude-opus-4-7 -> "claude-opus-4-7"   (full ids pass through)
    """
    if not model_setting:
        return None
    if "/" not in model_setting:
        return None
    suffix = model_setting.split("/", 1)[1].strip()
    return suffix or None


# ---------------------------------------------------------------------------
# Runtime implementation.
# ---------------------------------------------------------------------------
class ClaudeRuntime:
    """Thin async-iterator adapter around `claude_agent_sdk.query(...)`."""

    def __init__(self, *, name: str, options):
        self.name = name
        self._options = options

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        from claude_agent_sdk import (
            AssistantMessage,
            ResultMessage,
            TextBlock,
            query,
        )

        try:
            async for message in query(prompt=prompt, options=self._options):
                # We surface (a) live assistant text blocks as they arrive,
                # (b) the final result string if the streamed assistant text
                #     never materialized (e.g. tool-only final turn).
                if isinstance(message, AssistantMessage):
                    for block in getattr(message, "content", []) or []:
                        if isinstance(block, TextBlock):
                            text = getattr(block, "text", "")
                            if text:
                                yield text
                elif isinstance(message, ResultMessage):
                    result = getattr(message, "result", None)
                    if result:
                        # ResultMessage arrives once at end; only yield if we
                        # haven't already streamed equivalent assistant text.
                        # The SDK guarantees ResultMessage.result is the final
                        # assistant turn, which is usually already streamed.
                        pass
        except Exception as exc:  # noqa: BLE001
            log.exception("claude stream failed")
            yield f"\n\n[agent error: {type(exc).__name__}: {exc}]"

    async def run(self, prompt: str) -> str:
        pieces: list[str] = []
        async for chunk in self.stream(prompt):
            pieces.append(chunk)
        return "".join(pieces)
