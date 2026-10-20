"""Pre-shipped tool factories.

Each module exposes `make(ctx) -> list[FunctionTool]`. The factory takes a
HubContext (paths, loaded skills/knowledge, etc.) and returns ready-to-attach
tools whose closures already know the hub.

Tool names listed in agent frontmatter `tools: [...]` are resolved against
the combined registry of pre-shipped + hub-local tools.
"""
from __future__ import annotations

import logging

from . import (access_admin, connect_tools, current_time, feedback_tool, files, github_tools, grep_data, knowledge,
               render, report_tools, skills_tool, sources, web_http, workflow_tools)


def make_all(ctx) -> dict[str, object]:
    """Return {tool_name: FunctionTool} for every pre-shipped tool, scoped to ctx."""
    out: dict[str, object] = {}
    for module in (files, knowledge, sources, skills_tool, render, web_http, current_time, grep_data,
                   access_admin, workflow_tools, report_tools, connect_tools, github_tools, feedback_tool):
        for tool in module.make(ctx):
            if tool.name in out:   # a workflow named like a core tool never replaces it
                logging.getLogger(__name__).warning("tool %r is defined twice; keeping the first", tool.name)
                continue
            out[tool.name] = tool
    return out
