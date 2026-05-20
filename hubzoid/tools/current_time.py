"""current_time: return the wall-clock time as an ISO 8601 string.

LLMs do not have a reliable internal clock. Without this tool, "what's
the date" prompts hallucinate. Hubzoid ships this as a pre-shipped tool
so every hub has it for free.

Accepts any IANA timezone name via zoneinfo (Python stdlib >= 3.9).
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agents import function_tool


def make(ctx) -> list:  # noqa: ARG001
    @function_tool
    def current_time(zone: str = "UTC") -> str:
        """Return the current date and time as an ISO 8601 string.

        Args:
            zone: IANA timezone name, e.g. "UTC", "Asia/Kolkata",
                "America/New_York", "Europe/London". Defaults to UTC.

        Returns:
            ISO 8601 timestamp, e.g. "2026-05-20T14:32:01+05:30".
        """
        try:
            tz = ZoneInfo(zone)
        except ZoneInfoNotFoundError:
            return f"[current_time: unknown timezone {zone!r}]"
        return datetime.now(tz).isoformat(timespec="seconds")

    return [current_time]
