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
