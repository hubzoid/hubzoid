"""A resumed Claude session's cost is counted once.

Claude Code (SDK 0.2.178, CLI 2.1.318) reports ``total_cost_usd`` and
``model_usage`` for the whole session when a query resumes it, while ``usage``
holds only the turn. Hubzoid resumes a chat branch's bound session on every
turn, so each usage row held the session's running total and the Console
added the same money up again on every turn. The figures below are copied from
three real turns of one session (claude-haiku-5-5); no model calls here."""
from __future__ import annotations

import asyncio
import claude_agent_sdk as sdk
import pytest

from hubzoid import _request_ctx, claude_credentials as creds, factory_claude
from hubzoid.factory_claude import ClaudeRuntime, _turn_cost

MODEL = "claude-haiku-5-5"


def _result(total, usage, model_usage, *, error=False, session="s1"):
    return sdk.ResultMessage(subtype="error_during_execution" if error else "success", duration_ms=1,
                             duration_api_ms=1, is_error=error, num_turns=1, session_id=session,
                             result=None if error else "done", total_cost_usd=total, usage=usage,
                             model_usage={MODEL: model_usage})


def _u(i, o, cr, cw):
    return {"input_tokens": i, "output_tokens": o, "cache_read_input_tokens": cr, "cache_creation_input_tokens": cw}


def _mu(i, o, cr, cw, cost):
    return {"inputTokens": i, "outputTokens": o, "cacheReadInputTokens": cr,
            "cacheCreationInputTokens": cw, "costUSD": cost}


# Observed, SDK 0.2.178: usage is the turn's, model_usage and the cost the session's.
TURN1 = _result(0.00191043, _u(2, 3, 12373, 8925), _mu(2, 3, 12373, 8925, 0.00191043))
TURN2 = _result(0.00382806, _u(2, 3, 12373, 8961), _mu(4, 6, 24746, 17886, 0.00382806))
TURN3 = _result(0.00405505, _u(2, 3, 21309, 61), _mu(6, 9, 46055, 17947, 0.00405505))


def test_a_resumed_turn_records_only_its_own_cost():
    assert _turn_cost(TURN2, 0.00191043, True, MODEL) == pytest.approx(0.00191763)
    assert _turn_cost(TURN3, 0.00382806, True, MODEL) == pytest.approx(0.00022699)
    # the answering model is found from the costs when the reply named none
    assert _turn_cost(TURN3, 0.00382806, True) == pytest.approx(0.00022699)


def test_a_query_that_did_not_resume_keeps_its_total():
    assert _turn_cost(TURN1, None, False, MODEL) == 0.00191043  # a new session
    # A fresh turn whose model_usage also counts a background call of the same
    # model (more tokens than usage) is still the turn's own cost.
    background = _result(0.004, _u(2, 3, 0, 900), _mu(902, 12, 0, 900, 0.004))
    assert _turn_cost(background, None, False, MODEL) == 0.004
    assert _turn_cost(_result(None, _u(1, 1, 0, 0), _mu(9, 9, 0, 0, 0)), 0.5, True, MODEL) is None


def test_a_resumed_turn_on_another_model_still_subtracts_the_start():
    # The session's earlier turns ran on another model: this model's own
    # counts match the turn, but the total still holds the earlier turns.
    switched = _result(3.0, _u(100, 10, 0, 0), _mu(100, 10, 0, 0, 2.0))
    assert _turn_cost(switched, 1.0, True, MODEL) == pytest.approx(2.0)


def test_a_session_total_with_an_unknown_start_is_shared_by_tokens():
    # A binding saved before op_0082 has no cost: the turn's share of the total
    # is estimated, never the whole running total again.
    estimate = _turn_cost(TURN3, None, True, MODEL)
    assert 0 < estimate < 0.2 * TURN3.total_cost_usd
    # a start above the total (a session that began again) is not subtracted
    assert 0 < _turn_cost(TURN3, 0.9, True, MODEL) < 0.2 * TURN3.total_cost_usd


@pytest.fixture
def hub(tmp_path, monkeypatch):
    for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "DATABASE_URL",
                "HUBZOID_OPERATIONAL_DB", "HUBZOID_DEPLOYMENT", *creds.TOKEN_KEYS):
        monkeypatch.delenv(key, raising=False)
    return tmp_path


def _runtime(hub):
    opts = sdk.ClaudeAgentOptions(model="haiku", tools=[])
    rt = ClaudeRuntime(name="t", options=opts, hub_dir=hub)
    rt._options_for_turn = lambda: opts
    return rt


def _run_turn(hub, monkeypatch, results, *, resume="s1", resume_cost=None):
    recorded, seen = [], []

    async def query(*, prompt, options):
        seen.append(getattr(options, "resume", None))
        yield results[len(seen) - 1]

    monkeypatch.setattr(sdk, "query", query)
    monkeypatch.setattr(factory_claude._request_ctx, "record_usage", recorded.append)
    slot = _request_ctx.SessionSlot(resume_id=resume, delta_prompt="next" if resume else None,
                                    resume_cost=resume_cost)
    with _request_ctx.chat_scope("chat"), _request_ctx.native_session_scope(slot):
        asyncio.run(_runtime(hub).run("full history"))
    return recorded, seen, slot


def test_a_chat_turn_records_its_own_cost_and_keeps_the_session_total(hub, monkeypatch):
    recorded, seen, slot = _run_turn(hub, monkeypatch, [TURN2], resume_cost=0.00191043)
    assert seen == ["s1"]
    assert recorded[-1]["cost_usd"] == pytest.approx(0.00191763)
    assert slot.session_cost == pytest.approx(0.00382806)  # the next turn's start


def test_a_fresh_session_records_and_keeps_its_cost(hub, monkeypatch):
    recorded, seen, slot = _run_turn(hub, monkeypatch, [TURN1], resume=None, resume_cost=None)
    assert seen == [None]
    assert recorded[-1]["cost_usd"] == pytest.approx(0.00191043)
    assert slot.session_cost == pytest.approx(0.00191043)


def test_failover_attempts_on_a_resumed_session_count_its_start_once(hub, monkeypatch):
    for key, value in zip(creds.TOKEN_KEYS, ("fake-primary", "fake-secondary")):
        monkeypatch.setenv(key, value)
    # The primary is refused at its limit after a little work; the backup
    # resumes the same session. Both report the session's total.
    refused = _result(0.00192043, _u(1, 0, 0, 100), _mu(3, 3, 12373, 9025, 0.00192043), error=True)
    rate_limited = sdk.AssistantMessage(content=[sdk.TextBlock(text="Limit reached")], model=MODEL,
                                        error="rate_limit")
    calls = []

    async def query(*, prompt, options):
        calls.append(options.resume)
        if len(calls) == 1:
            yield rate_limited
            yield refused
        else:
            yield TURN2

    recorded = []
    monkeypatch.setattr(sdk, "query", query)
    monkeypatch.setattr(factory_claude._request_ctx, "record_usage", recorded.append)
    slot = _request_ctx.SessionSlot(resume_id="s1", delta_prompt="next", resume_cost=0.00191043)
    with _request_ctx.chat_scope("chat"), _request_ctx.native_session_scope(slot):
        assert asyncio.run(_runtime(hub).run("full history")) == "done"
    assert calls == ["s1", "s1"]
    # the backup's own 0.00191763 plus the refused attempt's own 0.00001000
    assert recorded[-1]["cost_usd"] == pytest.approx(0.00192763)
    # the next turn starts from the backup's session total, not the combined cost
    assert slot.session_cost == pytest.approx(0.00382806)
    assert _turn_cost(TURN3, slot.session_cost, True, MODEL) == pytest.approx(0.00022699)


def test_three_failover_attempts_count_each_attempts_own_cost(hub, monkeypatch):
    for key, value in zip(creds.TOKEN_KEYS, ("fake-primary", "fake-secondary", "fake-tertiary")):
        monkeypatch.setenv(key, value)
    # Prior 1.00. Two refused attempts each add 0.10 of their own; the third adds 0.50.
    def refused(total):
        return _result(total, _u(100, 0, 0, 0), _mu(200, 0, 0, 0, total), error=True)
    results = [refused(1.10), refused(1.10), _result(1.50, _u(150, 5, 0, 0), _mu(350, 5, 0, 0, 1.50))]
    rate_limited = sdk.AssistantMessage(content=[sdk.TextBlock(text="Limit reached")], model=MODEL,
                                        error="rate_limit")
    calls = []

    async def query(*, prompt, options):
        calls.append(options.resume)
        if len(calls) < 3:
            yield rate_limited
        yield results[len(calls) - 1]

    recorded = []
    monkeypatch.setattr(sdk, "query", query)
    monkeypatch.setattr(factory_claude._request_ctx, "record_usage", recorded.append)
    slot = _request_ctx.SessionSlot(resume_id="s1", delta_prompt="next", resume_cost=1.0)
    with _request_ctx.chat_scope("chat"), _request_ctx.native_session_scope(slot):
        assert asyncio.run(_runtime(hub).run("full history")) == "done"
    assert calls == ["s1", "s1", "s1"]
    assert recorded[-1]["cost_usd"] == pytest.approx(0.70)
    assert slot.session_cost == pytest.approx(1.50)
