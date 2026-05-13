"""Example hub-local tool.

Drop your own `@function_tool` callables into this folder. Files starting
with underscore (like `_template.py`) are skipped by the loader. Rename or
duplicate this file to expose a tool to your agents.

Reference the tool by its function name in any sub-agent's `tools:` list:
    tools: [reverse_string]
"""
from __future__ import annotations

from agents import function_tool


@function_tool
def reverse_string(text: str) -> str:
    """Reverse a string. Demonstrates how to add a custom tool to your hub.

    Args:
        text: The string to reverse.

    Returns:
        The reversed string.
    """
    return text[::-1]
