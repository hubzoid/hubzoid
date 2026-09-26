"""Usage rows name the model that answered, not the Claude CLI's background
call. The CLI's `model_usage` lists a small Haiku request on every turn, often
first (observed with claude-local on Sonnet: keys
['claude-haiku-4-5-20251001', 'claude-sonnet-5']), so the first key recorded
Sonnet chats as Haiku. No model calls."""
from __future__ import annotations

import asyncio

import claude_agent_sdk
from claude_agent_sdk import AssistantMessage, ResultMessage
from claude_agent_sdk.types import TextBlock

from hubzoid import factory_claude

OBSERVED = {  # the shape the CLI returned on a live claude-local/sonnet turn
    "claude-haiku-4-5-20251001": {"inputTokens": 899, "outputTokens": 9, "costUSD": 0.000944},
    "claude-sonnet-5": {"inputTokens": 2, "outputTokens": 4, "costUSD": 0.01354},
}


def _result(model_usage=OBSERVED):
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                         num_turns=1, session_id="s", result="OK",
                         usage={"input_tokens": 2, "output_tokens": 4}, model_usage=model_usage)


def test_the_answering_model_wins_over_the_background_call():
    assert factory_claude._answering_model(_result(), "claude-sonnet-5") == "claude-sonnet-5"
    # Without an assistant message, the entry that cost the most, not the first.
    assert factory_claude._answering_model(_result()) == "claude-sonnet-5"
    only = {"claude-haiku-4-5-20251001": OBSERVED["claude-haiku-4-5-20251001"]}
    assert factory_claude._answering_model(_result(only)) == "claude-haiku-4-5-20251001"  # a Haiku hub
    assert factory_claude._answering_model(_result({})) is None


def test_chat_turn_records_the_answering_model(monkeypatch):
    recorded = []
    monkeypatch.setattr(factory_claude._request_ctx, "record_usage", recorded.append)
    factory_claude._record_claude_usage(_result(), "claude-sonnet-5")
    assert recorded[0]["model"] == "claude-sonnet-5"


def test_call_llm_records_the_answering_model(monkeypatch):
    async def fake_query(*, prompt, options):
        yield AssistantMessage(content=[TextBlock(text="OK")], model="claude-sonnet-5")
        yield _result()

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query)
    text, usage = asyncio.run(factory_claude.claude_complete("x", model_setting="claude-local"))
    assert text == "OK" and usage["model"] == "claude-sonnet-5"


def test_streamed_chat_turn_records_the_answering_model(tmp_path, monkeypatch):
    from hubzoid.access import Identity, identity_scope

    hub = tmp_path / "support"
    hub.mkdir()
    (hub / "AGENTS.md").write_text("---\nname: support\ndescription: d\n---\nHelp.\n")
    for k in ("HUBZOID_DEPLOYMENT", "DATABASE_URL", "HUBZOID_BROWSER"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HUBZOID_OPERATIONAL_DB", f"sqlite:///{tmp_path / 'ops.db'}")
    rt = factory_claude.build_claude_runtime(hub)
    recorded = []
    monkeypatch.setattr(factory_claude._request_ctx, "record_usage", recorded.append)

    async def fake_query(*, prompt, options):
        yield AssistantMessage(content=[TextBlock(text="OK")], model="claude-sonnet-5")
        yield _result()

    monkeypatch.setattr(claude_agent_sdk, "query", fake_query, raising=False)

    async def go():
        with identity_scope(Identity.make("ana@example.org", surface="owui")):
            return "".join([p async for p in rt.stream("hello")])

    asyncio.run(go())
    assert [r["model"] for r in recorded] == ["claude-sonnet-5"]
