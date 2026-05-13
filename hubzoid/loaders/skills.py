"""Discover SKILL.md files under <hub>/skills/.

Each skill is a folder `<hub>/skills/<name>/SKILL.md`. Frontmatter:
  name:        skill identifier (used by load_skill(name))
  description: shown in the load_skill tool menu

A flatter shape is also accepted for convenience: `<hub>/skills/<name>.md`.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from .. import frontmatter
from .._fs import resolve_bucket


class SkillSpec(BaseModel):
    name: str = Field(..., min_length=1)
    description: str = Field(..., min_length=1)


@dataclass
class LoadedSkill:
    spec: SkillSpec
    body: str
    source_path: Path


def load_all(hub_dir: Path) -> list[LoadedSkill]:
    skills_dir = resolve_bucket(hub_dir, "skills")
    if skills_dir is None:
        return []

    out: list[LoadedSkill] = []
    # Folder-based: skills/<name>/SKILL.md (or skill.md)
    for child in sorted(skills_dir.iterdir(), key=lambda p: p.name.lower()):
        if child.is_dir() and not child.name.startswith("."):
            md = _find_skill_file(child)
            if md is not None:
                out.append(_load_one(md))
        elif child.is_file() and child.suffix.lower() == ".md" and not child.name.startswith("."):
            out.append(_load_one(child))
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
