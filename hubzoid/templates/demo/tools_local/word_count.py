"""Custom hub-local tool example: word_count.

Demonstrates how to add your own tool to a Hubzoid hub. Drop any file
into tools_local/ with one or more @function_tool callables and they
are auto-discovered at boot. Files starting with underscore are skipped.

Reference the tool by name in any sub-agent's `tools:` whitelist:

    tools: [word_count]

Delete this file once you have your own tools, or use it as a template.
"""
from __future__ import annotations

from agents import function_tool


@function_tool
def word_count(text: str) -> str:
    """Return a quick count of words, characters, and lines in a string.

    Args:
        text: Any string. Newlines are honored; multiple spaces collapse.

    Returns:
        Human-readable summary, e.g. "42 words, 251 chars, 6 lines".
    """
    words = len(text.split())
    chars = len(text)
    lines = text.count("\n") + 1 if text else 0
    return f"{words} words, {chars} chars, {lines} lines"
