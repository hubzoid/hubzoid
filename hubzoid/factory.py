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
    delegates: list = field(default_factory=list)


def build_agent(hub_dir: Path, *, extra_tools: dict[str, FunctionTool] | None = None,
                model_override: str | None = None) -> Agent:
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

    # Resolve the hub model first — it decides which sub-agents are delegates.
    # A caller-supplied override (a scheduled task's `model:`) wins over both
    # .env MODEL and AGENTS.md `model:`.
    main_spec = agents_loader.load_main(hub_dir)
    main_model_id = (model_override or "").strip() or settings.model or main_spec.spec.model
    if not main_model_id:
        raise RuntimeError(
            "no model configured. Set MODEL in <hub>/.env or `model:` in AGENTS.md frontmatter."
        )

    skills, delegate_agents = _load_skills_and_delegates(hub_dir, main_model_id)
    # Build each delegate's model up front so a missing provider key degrades
    # it to an inline skill (hub still boots) rather than crashing the build.
    kept, fallbacks = _prepare_delegates(delegate_agents)
    for loaded in fallbacks:
        skills.append(agents_loader.to_skill(loaded))
    skills = _with_core_skills(skills)     # chat runtime gets core skills
    knowledge = knowledge_loader.load_all(hub_dir)
    log.info(
        "hub %s: %d skill(s), %d delegate(s), %d knowledge doc(s)",
        hub_dir.name, len(skills), len(kept), len(knowledge),
    )

    ctx = HubContext(
        hub_dir=hub_dir,
        output_dir=output_dir,
        session_id=session_id,
        settings=settings,
        skills=skills,
        knowledge=knowledge,
        delegates=[loaded for loaded, _ in kept],
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
    _add_curator_tool(ctx, registry, access)

    mcp_servers = mcp_loader.load_all(hub_dir)

    # Delegates run as within-turn subagents the main agent calls (as_tool),
    # each on its own model. Built from the gated registry so their tool scope
    # respects restricted/.
    delegate_tools = _build_delegate_tools(kept, registry)

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
        tools=list(registry.values()) + delegate_tools,
        mcp_servers=mcp_servers,
        **extra,
    )
    return main


# ---------------------------------------------------------------------------
# Helpers shared with factory_claude.
# ---------------------------------------------------------------------------
def _with_core_skills(skills: list) -> list:
    """Merge hubzoid's core-shipped skills into a hub's skill list, lowest
    priority (a hub `skills/` or `agents/`-promoted skill of the same name
    wins). Called by the chat factories only — the MCP surface deliberately
    does not get core skills (they assume the chat runtime's tools)."""
    by_name = {s.spec.name: s for s in skills}
    for s in skills_loader.load_core():
        if s.spec.name in by_name:
            log.info("hub defines %r; the core-shipped skill is not used", s.spec.name)
            continue
        by_name[s.spec.name] = s
    return list(by_name.values())


def _add_curator_tool(ctx: HubContext, registry: dict, access) -> None:
    """Add the core-shipped, `curator`-gated `remember` tool to the registry.

    Present on every hub but hidden/denied unless the caller is in the hub's
    `curator` Open WebUI group on a verified-login surface (same wall as
    restricted/ tools). A hub that already defines its own tool of the same
    name keeps it — hub wins over the core tool.
    """
    from .tools import curator as curator_tool

    for ft in curator_tool.make(ctx):
        if ft.name in registry:
            log.info("hub tool %r overrides the core curator tool", ft.name)
            continue
        registry[ft.name] = access.guard_tool(ft, curator_tool.CURATOR_PERMISSION,
                                               ctx.hub_dir)


def _load_skills_and_delegates(hub_dir: Path, hub_model: str | None):
    """Return (skills, delegate_agents).

    skills = real skills/ + skill-classified sub-agents, deduped by name (real
    skills/ win on conflict, with a warning). delegate_agents = LoadedAgent
    objects whose `model:` differs from the hub on the same engine.
    """
    from . import handover

    # Precedence within the hub: hub `skills/` > `agents/`-promoted. Core-shipped
    # skills are NOT merged here — they're a chat-runtime concern added by the
    # chat factories via `_with_core_skills`, so the MCP surface (which also
    # calls this) does NOT advertise core skills like `dashboard` whose delivery
    # tool (write_artifact) it doesn't expose.
    real = skills_loader.load_hub(hub_dir)
    skill_agents, raw_delegates = agents_loader.split_subagents(hub_dir, hub_model)
    by_name: dict[str, object] = {s.spec.name: s for s in real}
    for loaded in skill_agents:
        s = agents_loader.to_skill(loaded)
        if s.spec.name in by_name:
            log.warning(
                "skill name collision: %r exists in both skills/ and agents/. "
                "skills/ wins (%s).",
                s.spec.name, by_name[s.spec.name].source_path,
            )
            continue
        by_name[s.spec.name] = s

    # Dedupe delegates by their handover tool-name (names can slug-collide,
    # e.g. "opus-helper" and "opus_helper"). First wins, mirroring skills.
    delegate_agents: list = []
    seen_tools: dict[str, object] = {}
    for loaded in raw_delegates:
        tname = handover.tool_name(loaded.spec.name)
        if tname in seen_tools:
            log.warning(
                "delegate tool-name collision: %r yields %r, already used by %s. "
                "keeping the first (%s).",
                loaded.spec.name, tname, seen_tools[tname], loaded.source_path,
            )
            continue
        seen_tools[tname] = loaded.source_path
        delegate_agents.append(loaded)
    return list(by_name.values()), delegate_agents


def _load_skills_and_promoted_agents(hub_dir: Path) -> list:
    """Back-compat: skills view with no delegate detection (all agents -> skills)."""
    return _load_skills_and_delegates(hub_dir, None)[0]


def _prepare_delegates(delegate_agents: list):
    """Build each delegate's model up front. Returns (kept, fallbacks).

    kept = [(LoadedAgent, LitellmModel)]; fallbacks = LoadedAgents whose model
    could not be built (e.g. missing provider key) — the caller demotes them to
    inline skills so the hub still boots.
    """
    kept: list = []
    fallbacks: list = []
    for loaded in delegate_agents:
        try:
            m = modellib.build(loaded.spec.model)
        except modellib.MissingProviderKey as exc:
            log.warning("delegate %r cannot run (%s); loading it as a skill instead.",
                        loaded.spec.name, exc)
            fallbacks.append(loaded)
        except Exception as exc:  # noqa: BLE001 — an optional delegate must never
            # harder-fail than the old skill path; degrade so the hub still boots.
            log.warning("delegate %r failed to build (%s); loading it as a skill instead.",
                        loaded.spec.name, exc)
            fallbacks.append(loaded)
        else:
            kept.append((loaded, m))
    return kept, fallbacks


def _build_delegate_tools(kept: list, registry: dict):
    """Wrap each kept delegate as an Agent.as_tool FunctionTool.

    The sub-agent runs on its own model in an isolated context; its final
    message returns as the tool result and the main agent keeps control.
    """
    from . import handover

    tools: list = []
    reg_names = list(registry.keys())
    for loaded, model in kept:
        scoped = handover.scoped_tool_names(loaded.spec.tools, reg_names)
        sub = Agent(
            name=loaded.spec.name,
            instructions=loaded.instructions,
            model=model,
            tools=[registry[n] for n in scoped],
        )
        tools.append(sub.as_tool(
            tool_name=handover.tool_name(loaded.spec.name),
            tool_description=loaded.spec.description,
        ))
    return tools


def _compose_instructions(body: str, ctx: HubContext, *, backend: str) -> str:
    """Append the Hubzoid runtime addendum to the user's AGENTS.md body.

    Honours the `auto_addendum: false` opt-out on the main agent.
    """
    if not system_addendum.is_enabled(ctx.hub_dir):
        return body
    return body.rstrip() + "\n\n" + system_addendum.build(ctx, backend=backend)
