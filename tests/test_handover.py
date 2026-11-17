from __future__ import annotations

import logging

import pytest

from hubzoid import handover


class TestEngine:
    @pytest.mark.parametrize("model,expected", [
        ("claude-local", "claude"),
        ("claude-local/opus", "claude"),
        ("CLAUDE-LOCAL/haiku", "claude"),
        ("openai/gpt-4o", "litellm"),
        ("openrouter/anthropic/claude-3-opus", "litellm"),
        ("", "litellm"),
        (None, "litellm"),
    ])
    def test_engine(self, model, expected):
        assert handover.engine(model) == expected


class TestResolveTier:
    @pytest.mark.parametrize("model,tier", [
        ("claude-local", "sonnet"),
        ("claude-local/sonnet", "sonnet"),
        ("claude-local/opus", "opus"),
        ("claude-local/haiku", "haiku"),
        ("claude-local/claude-opus-4-7", "claude-opus-4-7"),
        ("claude-local   ", "sonnet"),
    ])
    def test_resolve_tier(self, model, tier):
        assert handover.resolve_tier(model) == tier


class TestClassify:
    def test_no_model_is_skill(self):
        assert handover.classify(None, "claude-local") == "skill"
        assert handover.classify("", "openai/gpt-4o") == "skill"

    def test_same_claude_tier_is_skill(self):
        # bare claude-local == claude-local/sonnet
        assert handover.classify("claude-local", "claude-local") == "skill"
        assert handover.classify("claude-local/sonnet", "claude-local") == "skill"

    def test_different_claude_tier_is_delegate(self):
        assert handover.classify("claude-local/opus", "claude-local") == "delegate"
        assert handover.classify("claude-local/haiku", "claude-local/opus") == "delegate"

    def test_cross_engine_is_skill(self):
        # gpt sub inside a claude hub, and vice versa
        assert handover.classify("openai/gpt-4o", "claude-local") == "skill"
        assert handover.classify("claude-local/opus", "openai/gpt-4o") == "skill"

    def test_same_litellm_model_is_skill(self):
        assert handover.classify("openai/gpt-4o", "openai/gpt-4o") == "skill"

    def test_different_litellm_model_is_delegate(self):
        assert handover.classify(
            "openrouter/anthropic/claude-3-opus",
            "openrouter/anthropic/claude-haiku-4.5",
        ) == "delegate"

    def test_none_hub_model_is_skill(self):
        # hub_model unknown -> never delegate (old behavior)
        assert handover.classify("claude-local/opus", None) == "skill"


class TestToolName:
    def test_slug(self):
        assert handover.tool_name("Deep Researcher") == "handover_deep_researcher"
        assert handover.tool_name("opus-helper") == "handover_opus_helper"


class TestScopedToolNames:
    def test_whitelist_intersects_available(self, caplog):
        with caplog.at_level(logging.WARNING, logger="hubzoid.handover"):
            out = handover.scoped_tool_names(
                ["read_file", "does_not_exist"], ["read_file", "write_artifact"]
            )
        assert out == ["read_file"]
        assert any("unknown" in r.message for r in caplog.records)

    def test_empty_whitelist_returns_all_available(self):
        out = handover.scoped_tool_names([], ["read_file", "write_artifact"])
        assert out == ["read_file", "write_artifact"]


class TestSkillReason:
    """Why a sub-agent is loaded as a skill, for doctor and the load-time log."""

    @pytest.mark.parametrize("sub, hub, words", [
        (None, "claude-local", "names no model"),
        ("claude-local/opus", None, "over MCP"),
        ("openrouter/anthropic/claude-haiku-4.5", "claude-local", "different engine"),
        ("claude-local/sonnet", "claude-local", "same as the agent's"),
    ])
    def test_reasons(self, sub, hub, words):
        assert handover.classify(sub, hub) == "skill"
        assert words in handover.skill_reason(sub, hub)

    def test_a_delegate_has_no_reason(self):
        assert handover.skill_reason("claude-local/opus", "claude-local/sonnet") == ""


def _subagent(tmp_path, model):
    from hubzoid.loaders import agents as agents_loader

    hub = tmp_path / "hub"
    (hub / "agents").mkdir(parents=True)
    (hub / "AGENTS.md").write_text("---\nname: hub\n---\nMain.\n")
    (hub / "agents" / "reset.md").write_text(
        f"---\nname: reset\ndescription: Resets passwords.\nmodel: {model}\ntools: [read_knowledge]\n---\nReset.\n")
    return hub, agents_loader.load_subagents(hub)[0]


def test_the_mcp_path_does_not_warn_about_ignored_tools(tmp_path, caplog):
    """Over MCP every sub-agent is a skill by design; its warning was the same
    as a real misconfiguration's. The chat path still warns, with the reason."""
    from hubzoid import factory

    hub, _ = _subagent(tmp_path, "claude-local/opus")
    with caplog.at_level(logging.WARNING, logger="hubzoid.loaders.agents"):
        skills = factory._load_skills_and_promoted_agents(hub)
    assert [s.spec.name for s in skills] == ["reset"]
    assert not [r for r in caplog.records if "whitelist" in r.message]

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="hubzoid.loaders.agents"):
        factory._load_skills_and_delegates(hub, "claude-local/opus")
    (record,) = [r for r in caplog.records if "whitelist" in r.getMessage()]
    assert "same as the agent's" in record.getMessage()


def test_doctor_says_which_subagents_lost_their_tools(tmp_path, monkeypatch):
    from hubzoid import doctor

    monkeypatch.setenv("MODEL", "claude-local")
    hub, _ = _subagent(tmp_path, "openrouter/anthropic/claude-haiku-4.5")
    (check,) = doctor._subagent_checks(hub)
    assert check.id == "agents.mode" and check.status == "warn"
    assert any("reset: model: openrouter/anthropic/claude-haiku-4.5 and tools: ['read_knowledge']" in d
               for d in check.detail)

    (hub / "agents" / "reset.md").write_text(
        "---\nname: reset\ndescription: Resets.\nmodel: claude-local/opus\ntools: [read_knowledge]\n---\nReset.\n")
    (check,) = doctor._subagent_checks(hub)
    assert check.status == "ok" and check.detail == ["reset: runs on its own, on claude-local/opus"]
