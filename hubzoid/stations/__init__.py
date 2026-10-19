"""Isolated agents (stations) and handoff documents.

A station is an agent declared with `isolated: true` in
`agents/<name>/AGENTS.md`. A work-item workflow calls it for one stage:

    result = hub.call_agent(message, agent="analyst", stage="analysis")

It runs with its own instructions and exactly the tools it lists, on its own
engine, and returns a result checked against its `response_schema`. The call is
saved, so it never runs twice; the orchestrator may retry a failed call once
with `hub.retry_station(call_id, clarification)`. Longer documents pass between
stations of the same work item as handoffs (`save_handoff`, `read_handoff`).
"""
from __future__ import annotations

from .service import StationError, StationOutputError

__all__ = ["StationError", "StationOutputError"]
