"""Factory tests — build a real Agent against the minimal hub.

These tests do NOT call out to a model. We use a tiny .env that points at
OpenRouter (no key required at build time; the LLM is only called when an
agent runs).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
MINIMAL = FIXTURES / "minimal_hub"


@pytest.fixture(autouse=True)
def _env_model(monkeypatch):
    monkeypatch.setenv("MODEL", "openrouter/anthropic/claude-haiku-4.5")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-not-used-during-build")
    yield


def test_build_agent_minimal_hub():
    from hubzoid.factory import build_agent

    agent = build_agent(MINIMAL)
    assert agent.name == "testbot"
    # One sub-agent (echo) wired as handoff.
    assert len(agent.handoffs) == 1
    handoff = agent.handoffs[0]
    # The Agents SDK wraps Agents-as-handoffs; the underlying agent name is in `agent_name`.
    name = getattr(handoff, "agent_name", None) or getattr(handoff, "name", None)
    assert name == "echo"
    # Main agent has pre-shipped tools + tools_local (2) registered.
    tool_names = {getattr(t, "name", "") for t in agent.tools}
    assert {"read_file", "list_files", "write_artifact"}.issubset(tool_names)
    assert "list_skills" in tool_names and "load_skill" in tool_names
    assert "list_knowledge" in tool_names and "read_knowledge" in tool_names
    assert "render_jinja" in tool_names
    assert "http_get" in tool_names and "web_search" in tool_names
    assert "current_time" in tool_names
    assert "reverse_string" in tool_names  # from tools_local
    assert "sentinel_marker" in tool_names
    # Memory tools removed pending Item 6 redesign (per-user via OWUI headers).
    assert "remember" not in tool_names
    assert "recall" not in tool_names
    assert "forget" not in tool_names


def test_unknown_tool_in_subagent_raises(tmp_path, monkeypatch):
    (tmp_path / "AGENTS.md").write_text(
        "---\nname: main\ndescription: m\n---\nbody"
    )
    sub_dir = tmp_path / "agents" / "bad"
    sub_dir.mkdir(parents=True)
    (sub_dir / "AGENTS.md").write_text(
        "---\nname: bad\ndescription: oops\ntools: [does_not_exist]\n---\nbody"
    )

    from hubzoid.factory import build_agent

    with pytest.raises(RuntimeError, match="unknown names"):
        build_agent(tmp_path)


def test_missing_model_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("MODEL", raising=False)
    (tmp_path / "AGENTS.md").write_text(
        "---\nname: m\ndescription: d\n---\nbody"
    )
    from hubzoid.factory import build_agent

    with pytest.raises(RuntimeError, match="no model"):
        build_agent(tmp_path)


# ---------------------------------------------------------------------------
# _parse_model_pin — bare claude-local now defaults to Haiku for low TTFT.
# Explicit suffix overrides keep working.
# ---------------------------------------------------------------------------
class TestParseModelPin:
    def test_bare_claude_local_defaults_to_haiku(self):
        from hubzoid.factory_claude import _parse_model_pin
        assert _parse_model_pin("claude-local") == "haiku"

    def test_explicit_sonnet_pin_unchanged(self):
        from hubzoid.factory_claude import _parse_model_pin
        assert _parse_model_pin("claude-local/sonnet") == "sonnet"

    def test_explicit_opus_pin_unchanged(self):
        from hubzoid.factory_claude import _parse_model_pin
        assert _parse_model_pin("claude-local/opus") == "opus"

    def test_explicit_haiku_pin_unchanged(self):
        from hubzoid.factory_claude import _parse_model_pin
        assert _parse_model_pin("claude-local/haiku") == "haiku"

    def test_full_model_id_pin_unchanged(self):
        from hubzoid.factory_claude import _parse_model_pin
        assert _parse_model_pin("claude-local/claude-opus-4-7") == "claude-opus-4-7"

    def test_none_returns_none(self):
        from hubzoid.factory_claude import _parse_model_pin
        assert _parse_model_pin(None) is None

    def test_empty_string_returns_none(self):
        from hubzoid.factory_claude import _parse_model_pin
        assert _parse_model_pin("") is None

    def test_bare_claude_local_with_whitespace_still_defaults_to_haiku(self):
        from hubzoid.factory_claude import _parse_model_pin
        assert _parse_model_pin("claude-local  ") == "haiku"
