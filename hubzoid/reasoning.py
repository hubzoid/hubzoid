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


# How much of Claude's thinking to surface in the chat stream. Opus 4.7+ already
# thinks by default but returns the text "omitted" (signature-only), so the UI
# shows a dead spinner during the reasoning gap. These modes control what we do
# about that:
#   off       -> surface nothing (legacy behaviour; only an explicit
#                REASONING_EFFORT budget is applied, with no display).
#   indicator -> show a content-free "Thinking…" panel for the duration of the
#                reasoning, without exposing the reasoning text. Default.
#   full      -> stream the model's summarized reasoning text into the panel.
THINKING_MODES = ("off", "indicator", "full")


def normalize_thinking(raw: str | None) -> str:
    """Canonicalise SHOW_THINKING to off|indicator|full (default: indicator).

    Friendly aliases: true/text/on/reasoning -> full; false/none/no/0/disabled
    -> off. Unset or unrecognised -> indicator (fix the dead gap, expose nothing).
    """
    if raw is None:
        return "indicator"
    value = raw.strip().lower()
    if value in THINKING_MODES:
        return value
    if value in ("true", "text", "on", "yes", "reasoning"):
        return "full"
    if value in ("false", "none", "no", "0", "disabled"):
        return "off"
    return "indicator"


def claude_thinking_config(effort: str | None, mode: str) -> dict | None:
    """Build the `ClaudeAgentOptions.thinking` dict for a mode + effort.

    Returns None for 'off' (the caller may fall back to the legacy
    `max_thinking_tokens` knob). For indicator/full we request
    `display="summarized"` so the SDK streams `thinking_delta` events we can
    surface — without it, Opus 4.7+ returns signature-only (empty) thinking and
    the panel never fills. A REASONING_EFFORT budget pins an explicit thinking
    budget; otherwise the model thinks adaptively (no added latency versus the
    default it already runs).
    """
    if mode == "off":
        return None
    budget = claude_thinking_budget(effort)
    if budget is not None:
        return {"type": "enabled", "budget_tokens": budget, "display": "summarized"}
    return {"type": "adaptive", "display": "summarized"}
