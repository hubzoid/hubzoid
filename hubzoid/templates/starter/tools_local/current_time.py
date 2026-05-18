"""Custom hub-local tool: current_time.

Demonstrates how to add your own tool to a Hubzoid hub. Drop any file
into tools_local/ with one or more @function_tool callables and they
are auto-discovered at boot. Files starting with underscore are skipped.

Reference the tool by name in any sub-agent's `tools:` whitelist:

    tools: [current_time]
"""
from __future__ import annotations

from datetime import datetime, timezone

from agents import function_tool


@function_tool
def current_time(zone: str = "UTC") -> str:
    """Return the current date and time as an ISO 8601 string.

    LLMs do not have a reliable internal clock. Wrap timestamp access
    in a tool so the agent can quote the exact moment of a query.

    Args:
        zone: Timezone name. Only "UTC" is honored in this minimal
            implementation. Extend by importing zoneinfo if you need
            local zones.

    Returns:
        ISO 8601 timestamp string, e.g. "2026-05-18T14:32:01+00:00".
    """
    if zone.upper() != "UTC":
        return f"Only UTC is supported in this demo tool. Got: {zone}"
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
