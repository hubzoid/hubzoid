"""Load AGENTS.md files into Agents SDK Agent objects.

Convention:
  <hub>/AGENTS.md                          -> main agent
  <hub>/agents/<name>/AGENTS.md            -> sub-agent (handoff)

Frontmatter schema for both:
  name:        agent identifier; required
  description: one-line summary used as handoff trigger; required
  model:       optional LiteLLM model id; overrides .env MODEL
  tools:       optional list of tool names (whitelist). Only meaningful on sub-agents
               in v1 — the main agent always has the full pre-shipped + tools_local set
               plus the dynamic load_skill/read_knowledge tools.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from .. import frontmatter


class AgentSpec(BaseModel):
    name: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)
    model: str | None = None
    tools: list[str] = Field(default_factory=list)


@dataclass
class LoadedAgent:
    spec: AgentSpec
    instructions: str
    source_path: Path


def load_main(hub_dir: Path) -> LoadedAgent:
    """Load <hub>/AGENTS.md as the main agent."""
    path = hub_dir / "AGENTS.md"
    if not path.is_file():
        raise FileNotFoundError(
            f"No AGENTS.md at {path}. "
            f"Every hub needs an AGENTS.md at its root."
        )
    return _load_one(path)


def load_subagents(hub_dir: Path) -> list[LoadedAgent]:
    """Discover every <hub>/agents/<name>/AGENTS.md and load it as a sub-agent."""
    from .._fs import resolve_bucket
    agents_dir = resolve_bucket(hub_dir, "agents")
    if agents_dir is None:
        return []

    out: list[LoadedAgent] = []
    for child in sorted(agents_dir.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        # Sub-agent files: AGENTS.md, or <child>.md (case-insensitive)
        candidates = [
            child / "AGENTS.md",
            child / "agents.md",
            child / "Agents.md",
        ]
        match = next((c for c in candidates if c.is_file()), None)
        if match is None:
            # Allow a single .md file at this depth
            mds = sorted(child.glob("*.md"))
            if mds:
                match = mds[0]
        if match is None:
            continue
        out.append(_load_one(match))
    return out


def _load_one(path: Path) -> LoadedAgent:
    fm, body = frontmatter.read(path)
    if not body:
        raise ValueError(f"{path} has no body — write instructions below the frontmatter.")
    try:
        spec = AgentSpec(**fm)
    except ValidationError as exc:
        raise ValueError(
            f"{path}: invalid frontmatter — {exc.errors()[0]['msg']} "
            f"(field: {'.'.join(str(p) for p in exc.errors()[0]['loc'])})"
        ) from exc
    return LoadedAgent(spec=spec, instructions=body, source_path=path)
