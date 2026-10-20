"""What a hub folder holds, as counts, so a list of agents can show each
folder at a glance. Same discovery rules as the loaders (folder names through
`_fs`, knowledge as `*.md`, skills and sub-agents as folders or `.md` files),
but nothing is read, parsed or imported: tool files are counted, not loaded.
"""
from __future__ import annotations

import logging
from pathlib import Path

from .._fs import resolve_bucket

log = logging.getLogger(__name__)


def _visible(path: Path) -> bool:
    return not path.name.startswith((".", "_"))


def _entries(folder: Path | None, suffixes: tuple[str, ...]) -> int:
    """Visible sub-folders and files with one of `suffixes`, one level down."""
    if folder is None:
        return 0
    return sum(1 for child in folder.iterdir()
               if _visible(child) and (child.is_dir() or child.suffix.lower() in suffixes))


def counts(hub_dir: Path) -> dict[str, int]:
    """``{knowledge, skills, subagents, tool_files, connectors}`` for one hub.
    A folder that can't be read counts as empty; this never raises."""
    hub_dir = Path(hub_dir)
    out = {"knowledge": 0, "skills": 0, "subagents": 0, "tool_files": 0, "connectors": 0}
    try:
        knowledge = resolve_bucket(hub_dir, "knowledge")
        if knowledge is not None:
            out["knowledge"] = sum(1 for p in knowledge.rglob("*.md") if p.is_file())
        out["skills"] = _entries(resolve_bucket(hub_dir, "skills"), (".md",))
        out["subagents"] = _entries(resolve_bucket(hub_dir, "agents"), (".md",))
        tools = resolve_bucket(hub_dir, "tools_local")
        if tools is not None:
            out["tool_files"] = sum(1 for p in tools.iterdir() if _visible(p) and p.is_file() and p.suffix == ".py")
        from . import mcp as mcp_loader

        servers = mcp_loader.describe(hub_dir)["servers"]
        out["connectors"] = sum(1 for server in servers if server.get("source", "file") == "file")
    except OSError as exc:
        log.warning("could not count the contents of %s: %s", hub_dir, exc)
    return out
