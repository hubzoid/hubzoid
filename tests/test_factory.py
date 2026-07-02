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
    # Hubzoid no longer wires sub-agents as handoffs. agents/ are promoted
    # to skills and the agent loads them inline via load_skill.
    assert not agent.handoffs
    # Main agent has pre-shipped tools + tools_local (2) registered.
    tool_names = {getattr(t, "name", "") for t in agent.tools}
    assert {"read_file", "list_files", "write_artifact", "read_upload", "list_artifacts"}.issubset(tool_names)
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


def test_agents_folder_promoted_to_skills(monkeypatch):
    """A sub-agent's AGENTS.md should appear in the skill registry."""
    from hubzoid.factory import _load_skills_and_promoted_agents

    skills = _load_skills_and_promoted_agents(MINIMAL)
    names = {s.spec.name for s in skills}
    # `greet` is a real skill in skills/; `echo` is promoted from agents/.
    assert "greet" in names
    assert "echo" in names


def test_main_agent_instructions_include_addendum():
    """The system prompt must contain the auto-generated addendum sections."""
    from hubzoid.factory import build_agent

    agent = build_agent(MINIMAL)
    body = agent.instructions
    # Hubzoid contributes a runtime context header after the user's AGENTS.md.
    assert "Hubzoid runtime context" in body
    assert "## Environment" in body
    # Skills section appears because agents/ was promoted and greet exists.
    assert "## Skills available" in body
    assert "echo" in body or "greet" in body
    # Knowledge section appears because minimal_hub has knowledge/colors.md.
    assert "## Knowledge available" in body
    assert "colors" in body
    # Generic tool guidance, NOT domain-specific.
    assert "## How to use your tools" in body
    # Negative: should not mention read_knowledge explicitly (generic guidance).
    tools_section_start = body.index("## How to use your tools")
    tools_section = body[tools_section_start:]
    assert "read_knowledge" not in tools_section
    assert "load_skill" not in tools_section


def test_addendum_opt_out_via_frontmatter(tmp_path, monkeypatch):
    """`auto_addendum: false` on the main agent must suppress the addendum."""
    (tmp_path / "AGENTS.md").write_text(
        "---\nname: m\ndescription: d\nauto_addendum: false\n---\nplain body"
    )
    from hubzoid.factory import build_agent

    agent = build_agent(tmp_path)
    assert "Hubzoid runtime context" not in agent.instructions


def test_promoted_agent_tools_whitelist_ignored_with_warning(tmp_path, caplog):
    """Sub-agent `tools:` whitelists must be discarded when promoted to a skill."""
    (tmp_path / "AGENTS.md").write_text("---\nname: m\ndescription: d\n---\nbody")
    sub_dir = tmp_path / "agents" / "bad"
    sub_dir.mkdir(parents=True)
    (sub_dir / "AGENTS.md").write_text(
        "---\nname: bad\ndescription: oops\ntools: [does_not_exist]\n---\nbody"
    )

    import logging
    from hubzoid.loaders import agents as agents_loader

    with caplog.at_level(logging.WARNING, logger="hubzoid.loaders.agents"):
        skills = agents_loader.promote_to_skills(tmp_path)
    assert len(skills) == 1
    assert skills[0].spec.name == "bad"
    # And we logged the discarded whitelist so an operator can notice.
    assert any("tools whitelist" in r.message for r in caplog.records)


def test_real_skill_wins_over_promoted_agent_on_name_conflict(tmp_path, caplog):
    """If skills/foo and agents/foo both exist, skills/ wins."""
    (tmp_path / "AGENTS.md").write_text("---\nname: m\ndescription: d\n---\nbody")
    skill_dir = tmp_path / "skills" / "foo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: foo\ndescription: real skill\n---\nthis is the real body"
    )
    agent_dir = tmp_path / "agents" / "foo"
    agent_dir.mkdir(parents=True)
    (agent_dir / "AGENTS.md").write_text(
        "---\nname: foo\ndescription: promoted from agents/\n---\nagent body"
    )

    import logging
    from hubzoid.factory import _load_skills_and_promoted_agents

    with caplog.at_level(logging.WARNING, logger="hubzoid"):
        skills = _load_skills_and_promoted_agents(tmp_path)
    foo = next(s for s in skills if s.spec.name == "foo")
    assert "real body" in foo.body
    assert "agent body" not in foo.body
    assert any("collision" in r.message for r in caplog.records)


def test_missing_model_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("MODEL", raising=False)
    (tmp_path / "AGENTS.md").write_text(
        "---\nname: m\ndescription: d\n---\nbody"
    )
    from hubzoid.factory import build_agent

    with pytest.raises(RuntimeError, match="no model"):
        build_agent(tmp_path)


# ---------------------------------------------------------------------------
# Model-triggered delegation: a sub-agent whose model differs from the hub
# (same engine) becomes a `handover_<name>` tool instead of an inline skill.
# ---------------------------------------------------------------------------
def _write_hub_with_delegate(tmp_path, sub_model):
    (tmp_path / "AGENTS.md").write_text("---\nname: m\ndescription: d\n---\nbody")
    sub = tmp_path / "agents" / "opusguy"
    sub.mkdir(parents=True)
    (sub / "AGENTS.md").write_text(
        f"---\nname: opusguy\ndescription: hard questions\nmodel: {sub_model}\n"
        f"tools: [read_file]\n---\nYou are the specialist."
    )
    return tmp_path


def test_delegate_becomes_handover_tool(tmp_path, monkeypatch):
    # hub on haiku, delegate on a DIFFERENT same-engine model -> a handover tool.
    monkeypatch.setenv("MODEL", "openrouter/anthropic/claude-haiku-4.5")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    hub = _write_hub_with_delegate(tmp_path, "openrouter/anthropic/claude-3-opus")

    from hubzoid.factory import build_agent
    agent = build_agent(hub)
    tool_names = {getattr(t, "name", "") for t in agent.tools}
    assert "handover_opusguy" in tool_names
    # It must NOT also be wired as a handoff.
    assert not agent.handoffs


def test_delegate_missing_key_falls_back_to_skill(tmp_path, monkeypatch, caplog):
    # delegate model needs OPENAI_API_KEY which is absent -> skill fallback.
    monkeypatch.setenv("MODEL", "openrouter/anthropic/claude-haiku-4.5")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    hub = _write_hub_with_delegate(tmp_path, "openai/gpt-4o")  # cross-provider, same engine

    import logging
    from hubzoid.factory import build_agent
    with caplog.at_level(logging.WARNING, logger="hubzoid"):
        agent = build_agent(hub)
    tool_names = {getattr(t, "name", "") for t in agent.tools}
    assert "handover_opusguy" not in tool_names
    assert any("opusguy" in r.message for r in caplog.records)


def test_no_delegates_leaves_tool_list_unchanged():
    # Regression: minimal_hub (echo has no model) has NO handover tools.
    from hubzoid.factory import build_agent
    agent = build_agent(MINIMAL)
    tool_names = {getattr(t, "name", "") for t in agent.tools}
    assert not any(n.startswith("handover_") for n in tool_names)


def test_delegate_build_error_falls_back_to_skill(tmp_path, monkeypatch, caplog):
    # A non-MissingProviderKey build error must NOT crash the hub; degrade to skill.
    monkeypatch.setenv("MODEL", "openrouter/anthropic/claude-haiku-4.5")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    hub = _write_hub_with_delegate(tmp_path, "openrouter/anthropic/claude-3-opus")

    import hubzoid.model as modellib
    real_build = modellib.build

    def _selective(model_id):
        # Main model builds fine; only the delegate's model explodes.
        if model_id == "openrouter/anthropic/claude-3-opus":
            raise ValueError("adapter exploded")
        return real_build(model_id)

    monkeypatch.setattr("hubzoid.factory.modellib.build", _selective)

    import logging
    from hubzoid.factory import build_agent
    with caplog.at_level(logging.WARNING, logger="hubzoid"):
        agent = build_agent(hub)
    tool_names = {getattr(t, "name", "") for t in agent.tools}
    assert "handover_opusguy" not in tool_names
    assert any("failed to build" in r.message for r in caplog.records)


def test_delegate_slug_collision_deduped(tmp_path, monkeypatch, caplog):
    # Two delegates whose names slug to the same handover tool -> first wins.
    monkeypatch.setenv("MODEL", "openrouter/anthropic/claude-haiku-4.5")
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    (tmp_path / "AGENTS.md").write_text("---\nname: m\ndescription: d\n---\nbody")
    for folder in ("opus-helper", "opus_helper"):
        sub = tmp_path / "agents" / folder
        sub.mkdir(parents=True)
        (sub / "AGENTS.md").write_text(
            f"---\nname: {folder}\ndescription: d\n"
            f"model: openrouter/anthropic/claude-3-opus\n---\nbody"
        )

    import logging
    from hubzoid.factory import build_agent
    with caplog.at_level(logging.WARNING, logger="hubzoid"):
        agent = build_agent(tmp_path)
    handover_tools = [n for t in agent.tools
                      if (n := getattr(t, "name", "")).startswith("handover_")]
    assert handover_tools == ["handover_opus_helper"]
    assert any("collision" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# _parse_model_pin — bare claude-local now defaults to Sonnet. Haiku saved
# wall-clock TTFT but routinely asked the user to choose instead of executing
# documented workflows (the prs-agent QA pipeline reproducibly failed on Haiku
# and succeeded on Sonnet). Sonnet's decisiveness matters more than Haiku's
# latency for agentic tasks. Explicit suffix overrides keep working.
# ---------------------------------------------------------------------------
class TestParseModelPin:
    def test_bare_claude_local_defaults_to_sonnet(self):
        from hubzoid.factory_claude import _parse_model_pin
        assert _parse_model_pin("claude-local") == "sonnet"

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

    def test_bare_claude_local_with_whitespace_still_defaults_to_sonnet(self):
        from hubzoid.factory_claude import _parse_model_pin
        assert _parse_model_pin("claude-local  ") == "sonnet"
