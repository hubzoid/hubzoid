"""Discover knowledge/*.md under <hub>/knowledge/.

Each file is a single markdown document. Frontmatter is optional:
  name:        topic identifier (defaults to filename stem)
  description: shown in the read_knowledge / list_knowledge menus
  keywords:    optional list of search hints
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from .. import frontmatter
from .._fs import resolve_bucket

log = logging.getLogger("hubzoid")


@dataclass
class LoadedKnowledge:
    name: str
    description: str
    keywords: list[str] = field(default_factory=list)
    body: str = ""
    source_path: Path | None = None


def load_all(hub_dir: Path) -> list[LoadedKnowledge]:
    kdir = resolve_bucket(hub_dir, "knowledge")
    if kdir is None:
        return []

    out: list[LoadedKnowledge] = []
    for path in sorted(kdir.rglob("*.md"), key=lambda p: p.as_posix().lower()):
        if path.name.startswith(".") or path.name.lower() == "_index.md":
            continue
        # Isolate per-file failures: one malformed doc (e.g. broken YAML
        # frontmatter) must never blank the whole knowledge scan — skip and
        # log it, keep every good doc.
        try:
            fm, body = frontmatter.read(path)
        except Exception as exc:  # noqa: BLE001 — a bad file must not kill the load
            log.warning("knowledge: skipping unreadable %s: %s", path, exc)
            continue
        name = str(fm.get("name") or path.stem)
        desc = str(fm.get("description") or f"Knowledge document: {path.stem}.")
        kw = fm.get("keywords") or []
        if isinstance(kw, str):
            kw = [kw]
        out.append(
            LoadedKnowledge(
                name=name,
                description=desc,
                keywords=[str(k) for k in kw],
                body=body,
                source_path=path,
            )
        )
    return out
