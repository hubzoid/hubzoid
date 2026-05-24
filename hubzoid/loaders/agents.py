"""Load AGENTS.md files into Agents SDK Agent objects.

Convention:
  <hub>/AGENTS.md                          -> main agent
  <hub>/agents/<name>/AGENTS.md            -> sub-agent (handoff)

Frontmatter is OPTIONAL. AGENTS.md is a plain markdown file by default.
When frontmatter is missing, defaults are derived:
  name:        main agent  -> the hub folder name
               sub agent   -> the sub-agent's parent folder name
  description: first non-blank, non-heading line of the body (truncated at 200 chars)

Frontmatter schema when present:
  name:        agent identifier (optional)
  description: one-line summary used as handoff trigger for sub-agents (optional)
  model:       optional LiteLLM model id; overrides .env MODEL
  tools:       optional list of tool names (whitelist). Only meaningful on sub-agents
               in v1. The main agent always has the full pre-shipped plus tools_local set
               plus the dynamic load_skill / read_knowledge tools.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from .. import frontmatter


class AgentSpec(BaseModel):
    name: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    model: str | None = None
    tools: list[str] = Field(default_factory=list)
    # Quick-start prompts shown on the empty new-chat screen as click-to-send
    # buttons. Only honored on the main agent; ignored on sub-agents.
    suggestions: list[str] = Field(default_factory=list)


@dataclass
class LoadedAgent:
    spec: AgentSpec
    instructions: str
    source_path: Path


def load_main(hub_dir: Path) -> LoadedAgent:
    """Load <hub>/AGENTS.md as the main agent.

    name defaults to the hub folder name; description defaults to a derived
    summary from the body. Both can be overridden via frontmatter.
    """
    path = hub_dir / "AGENTS.md"
    if not path.is_file():
        raise FileNotFoundError(
            f"No AGENTS.md at {path}. "
            f"Every hub needs an AGENTS.md at its root."
        )
    return _load_one(path, default_name=_safe_id(hub_dir.name))


def load_subagents(hub_dir: Path) -> list[LoadedAgent]:
    """Discover sub-agent definitions under <hub>/agents/.

    Two layouts are supported, matching the skills loader's conventions:

      * Folder layout:  agents/<name>/AGENTS.md  (or agents/<name>/<anything>.md)
      * Flat layout:    agents/<name>.md

    The flat layout is useful for small specialist prompts where a whole
    folder per agent is overkill — the same way `skills/<name>.md` already
    works on the skills side. Folder layout is preferred when the agent
    has supporting files (templates, examples, scripts).
    """
    from .._fs import resolve_bucket
    agents_dir = resolve_bucket(hub_dir, "agents")
    if agents_dir is None:
        return []

    out: list[LoadedAgent] = []
    for child in sorted(agents_dir.iterdir(), key=lambda p: p.name.lower()):
        if child.name.startswith("."):
            continue

        if child.is_dir():
            # Folder layout. Prefer the conventional AGENTS.md; fall back
            # to any *.md if the author named the file differently.
            candidates = [
                child / "AGENTS.md",
                child / "agents.md",
                child / "Agents.md",
            ]
            match = next((c for c in candidates if c.is_file()), None)
            if match is None:
                mds = sorted(child.glob("*.md"))
                if mds:
                    match = mds[0]
            if match is None:
                continue
            out.append(_load_one(match, default_name=_safe_id(child.name)))
        elif child.is_file() and child.suffix.lower() == ".md":
            # Flat layout. The stem becomes the default name; frontmatter
            # `name:` still wins if present (handled in _load_one).
            out.append(_load_one(child, default_name=_safe_id(child.stem)))
    return out


def promote_to_skills(hub_dir: Path):
    """Load every <hub>/agents/<name>/AGENTS.md as a LoadedSkill.

    Hubzoid no longer treats `agents/` as a distinct primitive. Each
    sub-agent's body is loaded inline by the main agent when invoked via
    `load_skill(<name>)` — identical in mechanics to a real skill. This
    avoids handoff state bugs and gives the main agent stable control of
    the conversation across turns.

    The sub-agent's `tools:` whitelist is discarded with a log warning if
    present (skills do not gate tools; the main agent owns the registry).
    """
    import logging

    from .skills import LoadedSkill, SkillSpec

    log = logging.getLogger("hubzoid.loaders.agents")
    out: list = []
    for loaded in load_subagents(hub_dir):
        if loaded.spec.tools:
            log.warning(
                "%s: tools whitelist %r is ignored — agents/ are loaded as "
                "skills; the main agent owns all tools.",
                loaded.source_path, loaded.spec.tools,
            )
        spec = SkillSpec(
            name=loaded.spec.name,
            description=loaded.spec.description,
        )
        out.append(
            LoadedSkill(spec=spec, body=loaded.instructions, source_path=loaded.source_path)
        )
    return out


def _load_one(path: Path, *, default_name: str) -> LoadedAgent:
    fm, body = frontmatter.read(path)
    if not body:
        raise ValueError(f"{path} has no body. Write instructions in the file.")

    # Fill in optional fields with derived defaults so plain markdown works.
    fm = dict(fm)
    fm.setdefault("name", default_name)
    fm.setdefault("description", _derive_description(body, default_name))

    try:
        spec = AgentSpec(**fm)
    except ValidationError as exc:
        raise ValueError(
            f"{path}: invalid frontmatter. {exc.errors()[0]['msg']} "
            f"(field: {'.'.join(str(p) for p in exc.errors()[0]['loc'])})"
        ) from exc
    return LoadedAgent(spec=spec, instructions=body, source_path=path)


def _safe_id(name: str) -> str:
    """Turn an arbitrary folder name into a clean identifier."""
    out = re.sub(r"[^A-Za-z0-9_\-]+", "-", name.strip().lower())
    out = re.sub(r"-+", "-", out).strip("-")
    return out or "agent"


def _derive_description(body: str, fallback_name: str) -> str:
    """Pick the first non-blank, non-heading line as a one-line description."""
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            continue
        # Strip leading markdown decorators.
        line = re.sub(r"^[>*\-]+\s*", "", line)
        if line:
            return line[:200]
    return f"Agent: {fallback_name}."
