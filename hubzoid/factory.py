"""Top-level: build_agent(hub_dir) -> Agent.

Walks a hub folder, loads everything, and assembles an OpenAI Agents SDK
Agent with sub-agents (as handoffs), pre-shipped tools, hub-local tools,
skills/knowledge tools, and MCP servers.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from agents import Agent
from agents.tool import FunctionTool

from . import memory as memlib
from . import model as modellib
from . import settings as settingslib
from .loaders import agents as agents_loader
from .loaders import knowledge as knowledge_loader
from .loaders import mcp as mcp_loader
from .loaders import skills as skills_loader
from .loaders import tools_local as tools_local_loader
from .tools import make_all as make_builtin_tools

log = logging.getLogger("hubzoid")


@dataclass
class HubContext:
    hub_dir: Path
    output_dir: Path
    session_id: str
    settings: "settingslib.Settings"
    skills: list = field(default_factory=list)
    knowledge: list = field(default_factory=list)


def build_agent(hub_dir: Path) -> Agent:
    """Build and return the main Agent for the hub at `hub_dir`.

    Sub-agents are wired as handoffs. Tools are scoped per sub-agent based on
    each one's `tools:` frontmatter whitelist. Missing tool names raise with a
    list of valid names.
    """
    hub_dir = Path(hub_dir).resolve()
    if not hub_dir.is_dir():
        raise FileNotFoundError(f"hub directory not found: {hub_dir}")

    settings = settingslib.load(hub_dir)
    session_id = memlib.make_session_id()
    output_dir = memlib.session_output_dir(hub_dir, session_id)

    skills = skills_loader.load_all(hub_dir)
    knowledge = knowledge_loader.load_all(hub_dir)
    log.info(
        "hub %s: %d skill(s), %d knowledge doc(s)",
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

    # Tool registry: pre-shipped (with closures over ctx) + hub-local.
    builtin: dict[str, FunctionTool] = make_builtin_tools(ctx)  # name -> tool
    local: dict[str, FunctionTool] = tools_local_loader.load_all(hub_dir)
    overlap = set(builtin) & set(local)
    if overlap:
        log.info("hub-local tools override built-ins: %s", sorted(overlap))
    registry: dict[str, FunctionTool] = {**builtin, **local}

    mcp_servers = mcp_loader.load_all(hub_dir)

    # Sub-agents first; the main agent needs them as `handoffs=[...]`.
    sub_specs = agents_loader.load_subagents(hub_dir)
    handoffs: list[Agent] = [
        _build_one(spec, registry=registry, default_model=settings.model)
        for spec in sub_specs
    ]
    log.info("hub %s: %d sub-agent(s)", hub_dir.name, len(handoffs))

    main_spec = agents_loader.load_main(hub_dir)
    main_model_id = settings.model or main_spec.spec.model
    if not main_model_id:
        raise RuntimeError(
            "no model configured. Set MODEL in <hub>/.env or `model:` in AGENTS.md frontmatter."
        )
    main_model = modellib.build(main_model_id)

    # The main agent gets ALL tools (whitelist on the main agent is treated as full access).
    main_tools = list(registry.values())

    main = Agent(
        name=main_spec.spec.name,
        instructions=main_spec.instructions,
        model=main_model,
        tools=main_tools,
        handoffs=handoffs,
        mcp_servers=mcp_servers,
    )
    return main


def _build_one(loaded: agents_loader.LoadedAgent, *, registry: dict[str, FunctionTool], default_model: str | None) -> Agent:
    model_id = default_model or loaded.spec.model
    if not model_id:
        raise RuntimeError(
            f"{loaded.source_path}: no model. Set `model:` in frontmatter or MODEL in <hub>/.env."
        )

    tools: list[FunctionTool] = []
    if loaded.spec.tools:
        unknown = [t for t in loaded.spec.tools if t not in registry]
        if unknown:
            available = ", ".join(sorted(registry)) or "(none)"
            raise RuntimeError(
                f"{loaded.source_path}: tools reference unknown names: {unknown}. "
                f"Available: {available}"
            )
        tools = [registry[t] for t in loaded.spec.tools]
    # If no tools specified, sub-agent gets none (explicit-default-deny).

    return Agent(
        name=loaded.spec.name,
        handoff_description=loaded.spec.description,
        instructions=loaded.instructions,
        model=modellib.build(model_id),
        tools=tools,
    )
