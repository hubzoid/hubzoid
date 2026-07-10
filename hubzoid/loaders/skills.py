"""Discover SKILL.md files under <hub>/skills/ (plus core-shipped skills).

Each skill is a folder `<hub>/skills/<name>/SKILL.md`. Frontmatter:
  name:        skill identifier (used by load_skill(name))
  description: shown in the load_skill tool menu

A flatter shape is also accepted for convenience: `<hub>/skills/<name>.md`.

Core-shipped skills. Hubzoid ships a small set of platform skills under
`hubzoid/core_assets/skills/` (e.g. `dashboard`). Every hub gets these
automatically, WITHOUT copying them into the hub repo. A hub can override a
core skill by defining its own skill of the same `name` — the hub's wins, and
the core one is dropped (mirrors Claude Code's bundled-vs-on-disk precedence).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from .. import frontmatter
from .._fs import resolve_bucket

log = logging.getLogger("hubzoid")

# Platform skills shipped inside the package (not in any hub repo). This module
# lives at hubzoid/loaders/skills.py, so parents[1] is the hubzoid package dir.
_CORE_SKILLS_DIR = Path(__file__).resolve().parents[1] / "core_assets" / "skills"


class SkillSpec(BaseModel):
    name: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)


@dataclass
class LoadedSkill:
    spec: SkillSpec
    body: str
    source_path: Path


def load_hub(hub_dir: Path) -> list[LoadedSkill]:
    """Skills authored in the hub, under `<hub>/skills/`. No core skills.

    A malformed hub skill raises (fail loud) — it only affects that one hub.
    """
    return _scan_dir(resolve_bucket(hub_dir, "skills"))


def load_all(hub_dir: Path) -> list[LoadedSkill]:
    """Hub skills merged with core-shipped skills. Hub wins on name collision.

    Note: the effective precedence for the agent is set in
    `factory._load_skills_and_delegates`, which layers hub `skills/` >
    `agents/`-promoted > core. This helper is the two-layer (hub `skills/` >
    core) view used directly by callers/tests that don't have `agents/`.
    """
    by_name: dict[str, LoadedSkill] = {}
    for s in load_hub(hub_dir):               # hub skills first (own the menu order)
        by_name[s.spec.name] = s
    for s in load_core():
        if s.spec.name in by_name:
            log.info("hub skill %r overrides the core-shipped skill", s.spec.name)
            continue
        by_name[s.spec.name] = s
    return list(by_name.values())


def load_core() -> list[LoadedSkill]:
    """Skills shipped with the hubzoid package (available to every hub).

    Loaded RESILIENTLY: a malformed core skill is logged and skipped rather
    than raised, because a core skill loads for every hub — a bad one must
    never take down hubs that don't even use it (fleet blast radius).
    """
    return _scan_dir(_CORE_SKILLS_DIR if _CORE_SKILLS_DIR.is_dir() else None,
                     resilient=True)


def _scan_dir(skills_dir: Path | None, *, resilient: bool = False) -> list[LoadedSkill]:
    if skills_dir is None:
        return []
    out: list[LoadedSkill] = []
    # Folder-based: skills/<name>/SKILL.md (or skill.md)
    for child in sorted(skills_dir.iterdir(), key=lambda p: p.name.lower()):
        md: Path | None = None
        if child.is_dir() and not child.name.startswith("."):
            md = _find_skill_file(child)
        elif child.is_file() and child.suffix.lower() == ".md" and not child.name.startswith("."):
            md = child
        if md is None:
            continue
        try:
            out.append(_load_one(md))
        except (ValueError, OSError) as exc:
            if not resilient:
                raise
            log.warning("skipping malformed core skill %s: %s", md, exc)
    return out


def _find_skill_file(folder: Path) -> Path | None:
    for cand in ("SKILL.md", "skill.md", "Skill.md"):
        p = folder / cand
        if p.is_file():
            return p
    mds = sorted(folder.glob("*.md"))
    return mds[0] if mds else None


def _load_one(path: Path) -> LoadedSkill:
    fm, body = frontmatter.read(path)
    if not body:
        raise ValueError(f"{path}: skill has no body.")
    try:
        spec = SkillSpec(**fm)
    except ValidationError as exc:
        # Fallback: if name is missing, derive from filename / folder
        derived_name = path.parent.name if path.name.lower().startswith("skill") else path.stem
        fallback = dict(fm)
        fallback.setdefault("name", derived_name)
        fallback.setdefault("description", f"Skill loaded from {path.name}.")
        try:
            spec = SkillSpec(**fallback)
        except ValidationError:
            raise ValueError(
                f"{path}: invalid frontmatter — needs at least `name` and `description`. "
                f"({exc.errors()[0]['msg']})"
            ) from exc
    return LoadedSkill(spec=spec, body=body, source_path=path)
