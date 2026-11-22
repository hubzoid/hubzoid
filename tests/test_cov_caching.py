"""Prompt caching for LiteLLM-routed models (hubzoid.caching): which models
get cache marks, where the marks go (the system prompt and the last assistant
turn, never the newest user message), the TTL, the opt-out and the wrapper
installed around LiteLLM. Includes a property test on the marks."""
from __future__ import annotations

import asyncio
import copy

from hypothesis import given
from hypothesis import strategies as st

from hubzoid import caching


def test_which_models_are_marked(monkeypatch):
    assert caching._supports_cache_control(None) is False
    assert caching._supports_cache_control("openrouter/anthropic/claude-sonnet") is True
    assert caching._supports_cache_control("openai/gpt-x") is False
    import litellm.utils

    monkeypatch.setattr(litellm.utils, "supports_prompt_caching", lambda model: "claude" in model)
    assert caching._supports_cache_control("bedrock/anthropic.claude-x") is True
    assert caching._supports_cache_control("bedrock/meta.llama") is False

    def unknown(model):
        raise KeyError(model)

    monkeypatch.setattr(litellm.utils, "supports_prompt_caching", unknown)
    assert caching._supports_cache_control("bedrock/whatever") is False


def test_ttl(monkeypatch):
    monkeypatch.setattr(caching, "_CACHE_TTL", "1h")
    assert caching._cache_control_value() == {"type": "ephemeral", "ttl": "1h"}
    monkeypatch.setattr(caching, "_CACHE_TTL", "5m")
    assert caching._cache_control_value() == {"type": "ephemeral"}


def test_marks_go_on_the_system_prompt_and_the_last_assistant_turn(monkeypatch):
    monkeypatch.setattr(caching, "_CACHE_TTL", "1h")
    msgs = [{"role": "system", "content": "rules"},
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": [{"type": "text", "text": "a1"}, {"type": "image", "url": "x"}]},
            {"role": "user", "content": "q2"}]
    caching._inject(msgs, "anthropic/claude-x")
    assert msgs[0]["content"] == [{"type": "text", "text": "rules", "cache_control": {"type": "ephemeral", "ttl": "1h"}}]
    assert msgs[2]["content"][0]["cache_control"]["type"] == "ephemeral" and "cache_control" not in msgs[2]["content"][1]
    assert msgs[1]["content"] == "q1" and msgs[3]["content"] == "q2"     # user turns stay as they were
    other = copy.deepcopy(msgs)
    caching._inject(msgs, "openai/gpt-x")
    assert msgs == other
    assert caching._mark_last_text_block({"content": [{"type": "image"}]}) is False
    assert caching._mark_last_text_block({"content": None}) is False


_content = st.one_of(st.text(max_size=10), st.lists(st.fixed_dictionaries(
    {"type": st.sampled_from(["text", "image"]), "text": st.text(max_size=5)}), max_size=3), st.none())
_messages = st.lists(st.fixed_dictionaries({"role": st.sampled_from(["system", "user", "assistant", "tool"]),
                                            "content": _content}), max_size=8)


def _marks(messages):
    return [(i, m["role"]) for i, m in enumerate(messages) if isinstance(m["content"], list)
            for b in m["content"] if isinstance(b, dict) and "cache_control" in b]


@given(messages=_messages)
def test_at_most_two_marks_and_never_on_a_user_message(messages):
    caching._inject(messages, "anthropic/claude-x")
    marks = _marks(messages)
    assert len(marks) <= 2 and all(role in ("system", "assistant") for _, role in marks)
    assert len([r for _, r in marks if r == "system"]) <= 1 and len([r for _, r in marks if r == "assistant"]) <= 1


def test_install_wraps_litellm_once_and_honours_the_opt_out(monkeypatch):
    import litellm

    seen = []

    async def acompletion(*a, **kw):
        seen.append(("async", kw["messages"][0]["content"]))
        return "ok"

    def completion(*a, **kw):
        seen.append(("sync", kw["messages"][0]["content"]))
        return "ok"

    monkeypatch.setattr(litellm, "acompletion", acompletion)
    monkeypatch.setattr(litellm, "completion", completion)
    monkeypatch.setattr(caching, "_INSTALLED", False)
    monkeypatch.setenv("HUBZOID_DISABLE_PROMPT_CACHE", "true")
    caching.install()
    assert litellm.acompletion is acompletion                      # opted out: untouched

    monkeypatch.setattr(caching, "_INSTALLED", False)
    monkeypatch.delenv("HUBZOID_DISABLE_PROMPT_CACHE")
    caching.install()
    wrapped = litellm.acompletion
    caching.install()                                              # idempotent
    assert litellm.acompletion is wrapped and wrapped is not acompletion
    assert asyncio.run(litellm.acompletion(model="anthropic/claude-x", messages=[{"role": "system", "content": "s"}])) == "ok"
    assert litellm.completion("anthropic/claude-x", messages=[{"role": "system", "content": "s"}]) == "ok"
    assert all(isinstance(content, list) and "cache_control" in content[0] for _, content in seen)
