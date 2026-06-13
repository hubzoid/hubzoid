"""Translate one hub-level REASONING_EFFORT knob onto each backend.

OpenAI/Azure reasoning models take a discrete effort string (low|medium|high);
Claude extended thinking takes a token budget. Hubzoid exposes a single
setting and maps it per backend. Unset (None) means hubzoid passes nothing and
the model's own default applies — Azure keeps its built-in default effort,
Claude runs without extended thinking.
"""
from __future__ import annotations

EFFORTS = ("low", "medium", "high")

# low/medium/high -> Claude `max_thinking_tokens`. Claude has no discrete effort
# levels, so we map onto representative budgets (the API minimum is 1024).
_CLAUDE_BUDGET = {"low": 4_000, "medium": 12_000, "high": 24_000}


def normalize(raw: str | None) -> str | None:
    """Canonicalise an effort to low|medium|high, or None for unset/invalid."""
    if not raw:
        return None
    value = raw.strip().lower()
    return value if value in EFFORTS else None


def claude_thinking_budget(effort: str | None) -> int | None:
    """Map a canonical effort to a Claude `max_thinking_tokens` budget."""
    if not effort:
        return None
    return _CLAUDE_BUDGET.get(effort)
