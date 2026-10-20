"""What a hub folder holds, as counts, so a list of agents can show each
folder at a glance. Same discovery rules as the loaders (folder names through
`_fs`, knowledge as `*.md`, skills and sub-agents as folders or `.md` files),
but nothing is read, parsed or imported: tool files are counted, not loaded.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from .._fs import resolve_bucket

log = logging.getLogger(__name__)


def _visible(path: Path) -> bool:
    return not path.name.startswith((".", "_"))


def _inside(hub_dir: Path, folder: Path | None) -> Path | None:
    """The folder, when it really is inside the hub (a symlinked bucket that
    points elsewhere counts as empty)."""
    if folder is None:
        return None
    try:
        folder.resolve().relative_to(hub_dir.resolve())
    except ValueError:
        return None
    return folder


def _entries(folder: Path | None, suffixes: tuple[str, ...]) -> int:
    """Visible sub-folders and files with one of `suffixes`, one level down.
    Symlinks are not counted."""
    if folder is None:
        return 0
    return sum(1 for child in folder.iterdir()
               if _visible(child) and not child.is_symlink()
               and (child.is_dir() or child.suffix.lower() in suffixes))


def _markdown_files(folder: Path) -> int:
    """`*.md` files under `folder`, without following symlinks out of it."""
    total = 0
    for root, dirs, files in os.walk(folder, followlinks=False):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        total += sum(1 for name in files
                     if name.lower().endswith(".md") and not os.path.islink(os.path.join(root, name)))
    return total


def counts(hub_dir: Path) -> dict[str, int]:
    """``{knowledge, skills, subagents, tool_files, connectors}`` for one hub.
    Only the hub's own folders are read; a folder that can't be read counts as
    empty, and this never raises."""
    hub_dir = Path(hub_dir)
    out = {"knowledge": 0, "skills": 0, "subagents": 0, "tool_files": 0, "connectors": 0}
    try:
        knowledge = _inside(hub_dir, resolve_bucket(hub_dir, "knowledge"))
        if knowledge is not None:
            out["knowledge"] = _markdown_files(knowledge)
        out["skills"] = _entries(_inside(hub_dir, resolve_bucket(hub_dir, "skills")), (".md",))
        out["subagents"] = _entries(_inside(hub_dir, resolve_bucket(hub_dir, "agents")), (".md",))
        tools = _inside(hub_dir, resolve_bucket(hub_dir, "tools_local"))
        if tools is not None:
            out["tool_files"] = sum(1 for p in tools.iterdir()
                                    if _visible(p) and not p.is_symlink() and p.is_file() and p.suffix == ".py")
        from . import mcp as mcp_loader

        servers = mcp_loader.describe(hub_dir)["servers"]
        out["connectors"] = sum(1 for server in servers if server.get("source", "file") == "file")
    except OSError as exc:
        log.warning("could not count the contents of %s: %s", hub_dir, exc)
    return out
