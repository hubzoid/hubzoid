from __future__ import annotations

from agents import function_tool


@function_tool
def reverse_string(text: str) -> str:
    """Reverse a string."""
    return text[::-1]


@function_tool
def sentinel_marker() -> str:
    """Returns a sentinel string used by the test suite."""
    return "SENTINEL_42"
