"""Top-level: build_agent(hub_dir) -> Agent.

Walks a hub folder, loads everything, and assembles an OpenAI Agents SDK
Agent with pre-shipped tools, hub-local tools, skills + knowledge tools,
and MCP servers.

Sub-agents under `<hub>/agents/<name>/` are NOT wired as handoffs anymore.
They are promoted to skills at load time and loaded inline by the main
agent via `load_skill(<name>)`. See `loaders.agents.promote_to_skills`
for the rationale (handoff state didn't survive Hubzoid's stateless HTTP
bridge across turns).
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
from . import system_addendum
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


def build_agent(hub_dir: Path, *, extra_tools: dict[str, FunctionTool] | None = None) -> Agent:
    """Build and return the main Agent for the hub at `hub_dir`.

    All sub-agents from `<hub>/agents/<name>/` are promoted to skills and
    appended to the skill registry. On name collisions with real skills
    from `<hub>/skills/`, the explicit skill wins and a warning is logged.

    The main agent gets the full tool registry (pre-shipped + hub-local +
    MCP). The system prompt is the user's `AGENTS.md` body followed by a
    Hubzoid-generated addendum (knowledge index, skills index, generic
    tool guidance) — see `hubzoid.system_addendum`.

    `extra_tools` are caller-injected internals (scheduled-task runs) that
    win over both built-ins and hub-local tools on name conflicts.
    """
    hub_dir = Path(hub_dir).resolve()
    if not hub_dir.is_dir():
        raise FileNotFoundError(f"hub directory not found: {hub_dir}")

    settings = settingslib.load(hub_dir)
    session_id = memlib.make_session_id()
    output_dir = memlib.session_output_dir(hub_dir, session_id)

    skills = _load_skills_and_promoted_agents(hub_dir)
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
    builtin: dict[str, FunctionTool] = make_builtin_tools(ctx)
    local: dict[str, FunctionTool] = tools_local_loader.load_all(hub_dir)
    overlap = set(builtin) & set(local)
    if overlap:
        log.info("hub-local tools override built-ins: %s", sorted(overlap))
    registry: dict[str, FunctionTool] = {**builtin, **local, **(extra_tools or {})}

    # Gate access-controlled tools from <hub>/restricted/. No-op when the hub
    # has no restricted/ folder, so existing hubs are unchanged.
    from . import access  # deferred to avoid circular import via __init__.py
    registry = access.apply(hub_dir, registry)

    mcp_servers = mcp_loader.load_all(hub_dir)

    main_spec = agents_loader.load_main(hub_dir)
    main_model_id = settings.model or main_spec.spec.model
    if not main_model_id:
        raise RuntimeError(
            "no model configured. Set MODEL in <hub>/.env or `model:` in AGENTS.md frontmatter."
        )
    main_model = modellib.build(main_model_id)

    instructions = _compose_instructions(main_spec.instructions, ctx, backend="openai-agents")

    # Only override model_settings when an effort is configured, so the unset
    # case keeps the Agent's default ModelSettings (reasoning=None) and the
    # provider's own default applies.
    extra: dict = {}
    if settings.reasoning_effort:
        from agents import ModelSettings
        from openai.types.shared import Reasoning

        extra["model_settings"] = ModelSettings(
            reasoning=Reasoning(effort=settings.reasoning_effort)
        )

    main = Agent(
        name=main_spec.spec.name,
        instructions=instructions,
        model=main_model,
        tools=list(registry.values()),
        mcp_servers=mcp_servers,
        **extra,
    )
    return main


# ---------------------------------------------------------------------------
# Helpers shared with factory_claude.
# ---------------------------------------------------------------------------
def _load_skills_and_promoted_agents(hub_dir: Path) -> list:
    """Return real skills + promoted-agent skills, deduped by name.

    Real skills from `<hub>/skills/` win on conflicts. A warning is logged
    so the operator notices a name collision.
    """
    real = skills_loader.load_all(hub_dir)
    promoted = agents_loader.promote_to_skills(hub_dir)
    by_name: dict[str, object] = {s.spec.name: s for s in real}
    for s in promoted:
        if s.spec.name in by_name:
            log.warning(
                "skill name collision: %r exists in both skills/ and agents/. "
                "skills/ wins (%s).",
                s.spec.name, by_name[s.spec.name].source_path,
            )
            continue
        by_name[s.spec.name] = s
    return list(by_name.values())


def _compose_instructions(body: str, ctx: HubContext, *, backend: str) -> str:
    """Append the Hubzoid runtime addendum to the user's AGENTS.md body.

    Honours the `auto_addendum: false` opt-out on the main agent.
    """
    if not system_addendum.is_enabled(ctx.hub_dir):
        return body
    return body.rstrip() + "\n\n" + system_addendum.build(ctx, backend=backend)
