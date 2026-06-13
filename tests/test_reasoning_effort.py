"""REASONING_EFFORT: one hub-level knob mapped onto each backend.

OpenAI/Azure reasoning models take a discrete effort (low|medium|high) ->
ModelSettings(reasoning=Reasoning(effort=...)). Claude extended thinking takes
a token budget -> ClaudeAgentOptions(max_thinking_tokens=<budget>). Unset means
nothing is passed and the model's own default applies.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hubzoid import reasoning as reasoninglib

FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL = FIXTURES / "minimal_hub"


# ---------------------------------------------------------------------------
# Pure mapping helpers
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("low", "low"),
        ("medium", "medium"),
        ("high", "high"),
        ("HIGH", "high"),
        ("  Medium ", "medium"),
        (None, None),
        ("", None),
        ("ultra", None),
        ("none", None),
    ],
)
def test_normalize(raw, expected):
    assert reasoninglib.normalize(raw) == expected


@pytest.mark.parametrize(
    "effort,expected",
    [("low", 4000), ("medium", 12000), ("high", 24000), (None, None), ("bogus", None)],
)
def test_claude_thinking_budget(effort, expected):
    assert reasoninglib.claude_thinking_budget(effort) == expected


# ---------------------------------------------------------------------------
# settings.load
# ---------------------------------------------------------------------------
def test_settings_reads_reasoning_effort(monkeypatch, tmp_path):
    from hubzoid import settings as settingslib

    monkeypatch.setenv("REASONING_EFFORT", "High")  # normalised to "high"
    assert settingslib.load(tmp_path).reasoning_effort == "high"


def test_settings_reasoning_effort_unset_is_none(monkeypatch, tmp_path):
    from hubzoid import settings as settingslib

    monkeypatch.delenv("REASONING_EFFORT", raising=False)
    assert settingslib.load(tmp_path).reasoning_effort is None


# ---------------------------------------------------------------------------
# OpenAI/Azure factory wiring
# ---------------------------------------------------------------------------
@pytest.fixture
def _openai_env(monkeypatch):
    monkeypatch.setenv("MODEL", "openrouter/anthropic/claude-haiku-4.5")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-used-during-build")
    yield


def test_build_agent_sets_reasoning_effort(_openai_env, monkeypatch):
    from hubzoid.factory import build_agent

    monkeypatch.setenv("REASONING_EFFORT", "high")
    agent = build_agent(MINIMAL)
    assert agent.model_settings.reasoning is not None
    assert agent.model_settings.reasoning.effort == "high"


def test_build_agent_without_effort_leaves_reasoning_unset(_openai_env, monkeypatch):
    from hubzoid.factory import build_agent

    monkeypatch.delenv("REASONING_EFFORT", raising=False)
    agent = build_agent(MINIMAL)
    # Default ModelSettings -> reasoning None -> provider default applies.
    assert agent.model_settings.reasoning is None


# ---------------------------------------------------------------------------
# Claude factory wiring
# ---------------------------------------------------------------------------
@pytest.fixture
def _claude_env(monkeypatch):
    monkeypatch.setenv("MODEL", "claude-local")
    monkeypatch.setenv("BRIDGE_API_KEYS", "dev")
    yield


def test_build_claude_runtime_sets_thinking_budget(_claude_env, monkeypatch):
    from hubzoid.factory_claude import build_claude_runtime

    monkeypatch.setenv("REASONING_EFFORT", "medium")
    rt = build_claude_runtime(MINIMAL)
    assert rt._options.max_thinking_tokens == 12000


def test_build_claude_runtime_without_effort_leaves_budget_none(_claude_env, monkeypatch):
    from hubzoid.factory_claude import build_claude_runtime

    monkeypatch.delenv("REASONING_EFFORT", raising=False)
    rt = build_claude_runtime(MINIMAL)
    assert rt._options.max_thinking_tokens is None
