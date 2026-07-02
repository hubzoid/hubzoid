"""Classify agents/ sub-agents into skills vs model-delegates.

Pure functions, no I/O, no SDK imports. Both factories call these so the
skill-vs-delegate decision never drifts between backends.

A sub-agent becomes a *delegate* (runs on its own model, called by the main
agent as a within-turn subagent) only when its `model:` resolves to the SAME
engine as the hub AND differs from the hub's model. Otherwise it stays a
skill (loaded inline by the main agent) — the backward-compatible default.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger("hubzoid.handover")

_CLAUDE_PREFIX = "claude-local"
_CLAUDE_DEFAULT_TIER = "sonnet"  # mirror factory_claude._CLAUDE_LOCAL_DEFAULT


def engine(model_id: str | None) -> str:
    """Which runtime engine a model id belongs to."""
    return "claude" if (model_id or "").strip().lower().startswith(_CLAUDE_PREFIX) \
        else "litellm"


def resolve_tier(model_id: str | None) -> str:
    """The Claude tier pin for a claude-local id. Bare claude-local -> sonnet."""
    s = (model_id or "").strip()
    if "/" not in s:
        return _CLAUDE_DEFAULT_TIER
    suffix = s.split("/", 1)[1].strip()
    return suffix or _CLAUDE_DEFAULT_TIER


def norm(model_id: str | None) -> str:
    """Canonical identity used for equality comparison within one engine."""
    if engine(model_id) == "claude":
        return f"claude::{resolve_tier(model_id)}"
    return f"litellm::{(model_id or '').strip()}"


def classify(sub_model: str | None, hub_model: str | None) -> str:
    """Return 'delegate' or 'skill' for a sub-agent given the hub's model."""
    if not (sub_model or "").strip():
        return "skill"
    if not (hub_model or "").strip():
        return "skill"
    if engine(sub_model) != engine(hub_model):
        return "skill"
    if norm(sub_model) == norm(hub_model):
        return "skill"
    return "delegate"


def tool_name(agent_name: str) -> str:
    """The tool name the main agent uses to call a delegate."""
    slug = re.sub(r"[^a-z0-9]+", "_", (agent_name or "").strip().lower()).strip("_")
    return f"handover_{slug or 'agent'}"


def scoped_tool_names(whitelist: list[str], available: list[str]) -> list[str]:
    """Tool names a delegate may use.

    whitelist present -> intersection with `available` (unknown names dropped
    with a warning). whitelist empty -> all `available` (the recursion guard is
    automatic: delegate tools are never in `available`).
    """
    if not whitelist:
        return list(available)
    avail = set(available)
    allowed = [n for n in whitelist if n in avail]
    unknown = [n for n in whitelist if n not in avail]
    if unknown:
        log.warning("delegate tool whitelist has unknown tools (dropped): %s", unknown)
    return allowed
