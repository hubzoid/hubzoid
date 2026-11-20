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
    if (model_id or "").strip().lower().startswith("codex-local"):
        return "codex"
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
    if engine(model_id) == "codex":
        return "codex::" + ((model_id or "").strip().partition("/")[2] or "default")
    return f"litellm::{(model_id or '').strip()}"


def classify(sub_model: str | None, hub_model: str | None) -> str:
    """Return 'delegate' or 'skill' for a sub-agent given the hub's model."""
    return "skill" if skill_reason(sub_model, hub_model) else "delegate"


def skill_reason(sub_model: str | None, hub_model: str | None) -> str:
    """Why a sub-agent is a skill, in a few words ("" for a delegate)."""
    sub, hub = (sub_model or "").strip(), (hub_model or "").strip()
    if not sub:
        return "it names no model:"
    if not hub:
        return "it is offered over MCP, where the caller brings its own model"
    if engine(sub) != engine(hub):
        return (f"its model {sub!r} runs on a different engine than the agent's {hub!r}; "
                "a delegate must use the same engine")
    if norm(sub) == norm(hub):
        return f"its model {sub!r} is the same as the agent's {hub!r}"
    return ""


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
