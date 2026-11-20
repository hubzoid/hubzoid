"""A resumed Claude session's cost is counted once.

Claude Code (SDK 0.2.178, CLI 2.1.318) reports ``total_cost_usd`` and
``model_usage`` for the whole session when a query resumes it, while ``usage``
holds only the turn. Hubzoid resumes a chat branch's bound session on every
turn, so each usage row held the session's running total and the Console
added the same money up again on every turn. The figures below are copied from
three real turns of one session (claude-haiku-5-5); no model calls here."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import claude_agent_sdk as sdk
import pytest

from hubzoid import _request_ctx, budgets, claude_credentials as creds, factory_claude
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
# Observed, SDK 0.2.152: a resumed turn reported only itself.
OLD_TURN2 = _result(0.0063756, _u(10, 47, 18546, 2138), _mu(10, 47, 18546, 2138, 0.0063756))


def test_a_resumed_turn_records_only_its_own_cost():
    assert _turn_cost(TURN2, 0.00191043, MODEL) == pytest.approx(0.00191763)
    assert _turn_cost(TURN3, 0.00382806, MODEL) == pytest.approx(0.00022699)
    # the answering model is found from the costs when the reply named none
    assert _turn_cost(TURN3, 0.00382806) == pytest.approx(0.00022699)


def test_figures_for_the_turn_alone_are_kept():
    assert _turn_cost(TURN1, None, MODEL) == 0.00191043  # a new session
    assert _turn_cost(OLD_TURN2, 0.015, MODEL) == 0.0063756  # an older CLI, prior ignored
    assert _turn_cost(_result(None, _u(1, 1, 0, 0), _mu(9, 9, 0, 0, 0)), 0.5, MODEL) is None


def test_a_session_total_with_an_unknown_start_is_shared_by_tokens():
    # A binding saved before op_0082 has no cost: the turn's share of the total
    # is estimated, never the whole running total again.
    estimate = _turn_cost(TURN3, None, MODEL)
    assert 0 < estimate < 0.2 * TURN3.total_cost_usd
    # a start above the total (a session that began again) is not subtracted
    assert 0 < _turn_cost(TURN3, 0.9, MODEL) < 0.2 * TURN3.total_cost_usd


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


def test_a_run_cap_starts_from_the_resumed_sessions_total(monkeypatch):
    ticket = SimpleNamespace(max_turns=None, max_budget_usd=2.0)
    monkeypatch.setattr(budgets, "current", lambda: ticket)
    opts = sdk.ClaudeAgentOptions(model="haiku", resume="s1")
    slot = _request_ctx.SessionSlot(resume_id="s1", delta_prompt="next", resume_cost=40.0)
    with _request_ctx.native_session_scope(slot):
        assert factory_claude._budget_caps(opts).max_budget_usd == pytest.approx(42.0)
        fresh = sdk.ClaudeAgentOptions(model="haiku")
        assert factory_claude._budget_caps(fresh).max_budget_usd == pytest.approx(2.0)
    assert factory_claude._budget_caps(opts).max_budget_usd == pytest.approx(2.0)  # no slot
