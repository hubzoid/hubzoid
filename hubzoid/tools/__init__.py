"""Pre-shipped tool factories.

Each module exposes `make(ctx) -> list[FunctionTool]`. The factory takes a
HubContext (paths, loaded skills/knowledge, etc.) and returns ready-to-attach
tools whose closures already know the hub.

Tool names listed in agent frontmatter `tools: [...]` are resolved against
the combined registry of pre-shipped + hub-local tools.
"""
from __future__ import annotations

from . import files, knowledge, memory, render, skills_tool, web_http


def make_all(ctx) -> dict[str, object]:
    """Return {tool_name: FunctionTool} for every pre-shipped tool, scoped to ctx."""
    out: dict[str, object] = {}
    for module in (files, knowledge, skills_tool, memory, render, web_http):
        for tool in module.make(ctx):
            out[tool.name] = tool
    return out
