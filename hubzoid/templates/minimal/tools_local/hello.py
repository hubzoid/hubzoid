"""Starter tool: hello.

Hubzoid auto-discovers any module under tools_local/ at boot and exposes
its @function_tool callables to the agent. Files starting with an
underscore are skipped. Delete this file once you have your own tools.
"""
from __future__ import annotations

from agents import function_tool


@function_tool
def hello(name: str = "there") -> str:
    """Return a one-line greeting from the hub.

    Args:
        name: Who to greet. Defaults to "there".

    Returns:
        A short greeting string.
    """
    return f"Hello {name}, from your hub."
