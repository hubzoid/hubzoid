"""Usage limits (budgets.yaml): the file, admission before a model call,
reservations, and the runtime caps. No model and no network."""
from __future__ import annotations

import asyncio
import time

import pytest
from sqlalchemy import text

from hubzoid import budgets, db, migrations
from hubzoid.work_items.errors import WorkItemError


@pytest.fixture
def hub_dir(tmp_path, monkeypatch):
    for var in ("DATABASE_URL", "HUBZOID_OPERATIONAL_DB"):
        monkeypatch.delenv(var, raising=False)
    root = tmp_path / "desk"
    root.mkdir()
    (root / "AGENTS.md").write_text("---\nname: Desk\nmodel: claude-local\n---\nA desk.\n")
    return root


def limits(hub_dir, body: str) -> None:
    (hub_dir / "budgets.yaml").write_text("version: 1\nlimits:\n" + body)


def spend(hub_dir, *, run="r1", cost=None, out=0, model="claude-local"):
    engine = db.operational_engine(hub_dir)
    migrations.upgrade(engine, "operational")
    with engine.begin() as c:
        c.execute(text("INSERT INTO hz_usage (ts, hub, surface, kind, subject, chat_id, model, input_tokens, "
                       "output_tokens, cost_usd, status) VALUES (:t, :h, 'api', 'chat', 'a', :r, :m, 10, :o, :c, "
                       "'ok')"), {"t": time.time(), "h": hub_dir.name, "r": run, "m": model, "o": out, "c": cost})


def test_no_file_limits_nothing_and_errors_name_the_line(hub_dir):
    assert budgets.admit(hub_dir, model="codex-local") is None
    with pytest.raises(WorkItemError) as err:
        budgets.parse("version: 1\nlimits:\n  month:\n    max_cost_usd: \"-1\"\n")
    assert err.value.code == "invalid_budgets" and err.value.fields["line"] == 4
    with pytest.raises(WorkItemError, match="not supported"):
        budgets.parse("version: 1\nallocations: []\n")
    with pytest.raises(WorkItemError, match="month takes"):
        budgets.parse("version: 1\nlimits:\n  month:\n    max_turns: 3\n")
    assert budgets.parse("version: 1\nlimits:\n  run:\n    max_turns: 3\n").limits == {"run": {"max_turns": 3}}


def test_a_used_up_scope_refuses_before_the_call(hub_dir):
    limits(hub_dir, "  month:\n    max_cost_usd: \"1.00\"\n    max_output_tokens: 500\n")
    assert budgets.admit(hub_dir, model="claude-local") is not None
    spend(hub_dir, cost=0.4, out=100)
    ticket = budgets.admit(hub_dir, model="claude-local")
    assert ticket.max_budget_usd == pytest.approx(0.6)            # the runtime may spend only what is left
    spend(hub_dir, cost=0.7, out=100)
    with pytest.raises(WorkItemError) as err:
        budgets.admit(hub_dir, model="claude-local")
    assert err.value.code == "budget_exhausted" and err.value.fields["scope"] == "month"


def test_dollar_limits_need_a_price_and_token_limits_do_not(hub_dir):
    limits(hub_dir, "  month:\n    max_cost_usd: \"5\"\n")
    with pytest.raises(WorkItemError) as err:
        budgets.admit(hub_dir, model="codex-local")              # a Codex login reports no cost
    assert err.value.code == "unpriced_model"
    limits(hub_dir, "  month:\n    max_output_tokens: 1000\n")
    assert budgets.admit(hub_dir, model="codex-local") is not None


def test_running_calls_share_what_is_left(hub_dir):
    limits(hub_dir, "  run:\n    max_cost_usd: \"3\"\n  month:\n    max_cost_usd: \"5\"\n")
    first = budgets.admit(hub_dir, model="claude-local", run_id="a")
    second = budgets.admit(hub_dir, model="claude-local", run_id="b")
    assert (first.max_budget_usd, second.max_budget_usd) == (3.0, 2.0)
    with pytest.raises(WorkItemError):
        budgets.admit(hub_dir, model="claude-local", run_id="c")   # both are still running
    budgets.settle(hub_dir, first)
    assert budgets.admit(hub_dir, model="claude-local", run_id="d").max_budget_usd == 3.0


def test_a_work_item_counts_all_its_runs(hub_dir):
    item = "0f0e0d0c-0b0a-4908-8706-050403020100"
    limits(hub_dir, "  work_item:\n    max_output_tokens: 150\n")
    spend(hub_dir, run=f"wi-{item}-e1", out=100)
    assert budgets.admit(hub_dir, model="claude-local", run_id=f"wi-{item}-e2") is not None
    spend(hub_dir, run=f"wi-{item}-e2", out=60)
    with pytest.raises(WorkItemError) as err:
        budgets.admit(hub_dir, model="claude-local", run_id=f"wi-{item}-e3")
    assert err.value.fields["scope"] == "work_item"
    assert budgets.admit(hub_dir, model="claude-local", run_id="chat-1") is not None   # other work is not limited


def test_an_unsettled_reservation_holds_until_released(hub_dir):
    limits(hub_dir, "  run:\n    max_output_tokens: 100\n  month:\n    max_output_tokens: 100\n")
    lost = budgets.admit(hub_dir, model="claude-local", run_id="crashed")
    with pytest.raises(WorkItemError):
        budgets.admit(hub_dir, model="claude-local", run_id="next")
    assert budgets.status(hub_dir)["open_reservations"][0]["id"] == lost.id
    with pytest.raises(WorkItemError):
        budgets.release(hub_dir, lost.id, "cli:admin", reason="")
    budgets.release(hub_dir, lost.id, "cli:admin", reason="Provider shows no charge.")
    assert budgets.admit(hub_dir, model="claude-local", run_id="next") is not None


def test_apply_checks_the_revision(hub_dir):
    new = "version: 1\nlimits:\n  run:\n    max_turns: 5\n"
    with pytest.raises(WorkItemError) as err:
        budgets.apply(hub_dir, new, expected_revision="wrong")
    assert err.value.code == "revision_conflict" and err.value.fields["current_revision"] == "absent"
    out = budgets.apply(hub_dir, new, expected_revision="absent")
    assert budgets.load(hub_dir).revision == out["revision"]


def test_a_refused_turn_is_one_plain_notice(hub_dir):
    limits(hub_dir, "  month:\n    max_output_tokens: 10\n")
    spend(hub_dir, out=20)
    refused, ran = [], []

    async def inner():
        ran.append(True)
        yield "answer"

    async def collect():
        return [item async for item in budgets.guarded(hub_dir, "claude-local", inner, refused.append)]

    items = asyncio.run(collect())
    assert ran == [] and refused[0].code == "budget_exhausted"
    assert items[0].error_kind == "budget" and "used up" in items[0].text


def test_caps_reach_the_claude_options_and_name_the_stop():
    from claude_agent_sdk import ClaudeAgentOptions

    from hubzoid.factory_claude import _budget_caps

    token = budgets._CURRENT.set(budgets.Ticket(id=None, run_id="r", item_id=None, max_turns=4, max_budget_usd=0.5))
    try:
        capped = _budget_caps(ClaudeAgentOptions(max_turns=20))
    finally:
        budgets._CURRENT.reset(token)
    assert (capped.max_turns, capped.max_budget_usd) == (4, 0.5)
    assert _budget_caps(ClaudeAgentOptions(max_turns=20)).max_turns == 20        # no budgets: unchanged


def test_a_stop_at_the_run_limit_says_how_to_continue(hub_dir):
    from hubzoid.run_events import Notice

    limits(hub_dir, "  run:\n    max_turns: 2\n")

    async def inner():
        yield "partial"
        # The words the Claude CLI really uses when a run hits its turn cap.
        yield Notice(kind="error", text="ResultError: Claude Code returned an error result: Reached maximum "
                     "number of turns (2) (exit code: 1)", legacy="\n\n[agent error: ...]")

    async def collect():
        return [item async for item in budgets.guarded(hub_dir, "claude-local", inner, lambda e: None)]

    items = asyncio.run(collect())
    assert items[0] == "partial" and items[1].error_kind == "run_limit" and "continue" in items[1].text


def test_a_hold_settles_only_with_its_usage_row(hub_dir):
    from hubzoid import _request_ctx, usage

    limits(hub_dir, "  run:\n    max_output_tokens: 100\n  month:\n    max_output_tokens: 150\n")

    async def inner():
        yield "answer"

    async def turn():
        with _request_ctx.chat_scope("chat-1"):
            items = [item async for item in budgets.guarded(hub_dir, "claude-local", inner, lambda e: None)]
            return items, _request_ctx.drain_usage()

    _, raw = asyncio.run(turn())
    assert budgets.status(hub_dir)["open_reservations"]          # handed to the caller, still held
    usage.record(hub_dir, hub=hub_dir.name, surface="web", kind="chat", subject="a", chat_id="chat-1",
                 model="claude-local", output_tokens=20, reservation=raw["budget_reservation"])
    assert budgets.status(hub_dir)["open_reservations"] == []    # settled with the row that records it


def test_a_workflow_call_cut_off_by_its_deadline_settles_its_hold(hub_dir, monkeypatch):
    from hubzoid import runtime
    from hubzoid.workflows import deadlines

    limits(hub_dir, "  run:\n    max_output_tokens: 100\n")

    class Slow:
        last_error = None

        async def aopen(self):
            pass

        async def aclose(self):
            pass

        async def stream_events(self, prompt):
            async def inner():
                await asyncio.sleep(30)
                yield "never"

            from contextlib import aclosing

            # As the runtimes do: closed at once, so the hold reaches the usage row.
            async with aclosing(budgets.guarded(hub_dir, "claude-local", inner, lambda e: None)) as items:
                async for item in items:
                    yield item

    monkeypatch.setattr(runtime, "build", lambda hd, **kw: Slow())
    with deadlines.scope(absolute=time.time() + 1, check_exit=False), pytest.raises(BaseException):
        runtime.run_once(hub_dir, "hi")
    assert budgets.status(hub_dir)["open_reservations"] == []    # the usage row settled it


def test_a_deadline_during_admission_leaves_no_hold(hub_dir, monkeypatch):
    """The deadline can fall while the hold is being written on its thread:
    the thread still writes it, so it must still be settled, never left open
    (this is the race behind the occasional failure of the test above)."""
    from hubzoid import runtime
    from hubzoid.workflows import deadlines

    limits(hub_dir, "  run:\n    max_output_tokens: 100\n")
    real_admit = budgets.admit
    deadline = time.time() + 2
    admitted = []

    def slow_admit(*a, **kw):
        time.sleep(max(0.0, deadline - time.time()) + 0.5)   # still writing when the deadline falls
        admitted.append(1)
        return real_admit(*a, **kw)

    monkeypatch.setattr(budgets, "admit", slow_admit)

    class Never:
        last_error = None

        async def aopen(self):
            pass

        async def aclose(self):
            pass

        async def stream_events(self, prompt):
            async def inner():
                await asyncio.sleep(30)
                yield "never"

            from contextlib import aclosing

            async with aclosing(budgets.guarded(hub_dir, "claude-local", inner, lambda e: None)) as items:
                async for item in items:
                    yield item

    monkeypatch.setattr(runtime, "build", lambda hd, **kw: Never())
    with deadlines.scope(absolute=deadline, check_exit=False), pytest.raises(BaseException):
        runtime.run_once(hub_dir, "hi")
    assert admitted, "the deadline must fall while the hold is being written"
    assert budgets.status(hub_dir)["open_reservations"] == []


def test_a_hold_from_last_month_still_counts(hub_dir):
    limits(hub_dir, "  run:\n    max_output_tokens: 100\n  month:\n    max_output_tokens: 100\n")
    held = budgets.admit(hub_dir, model="claude-local", run_id="late-night")
    with budgets._engine(hub_dir).begin() as c:
        c.execute(text("UPDATE hz_budget_reservations SET month='2026-09' WHERE id=:i"), {"i": held.id})
    with pytest.raises(WorkItemError):
        budgets.admit(hub_dir, model="claude-local", run_id="new-month")


def test_amounts_must_be_finite():
    for bad in ('"NaN"', '"Infinity"', '"-0.5"'):
        with pytest.raises(WorkItemError):
            budgets.parse(f"version: 1\nlimits:\n  month:\n    max_cost_usd: {bad}\n")


def test_the_eval_judge_is_checked_and_bounded(hub_dir, monkeypatch):
    from hubzoid.evals import judge

    limits(hub_dir, "  run:\n    max_cost_usd: \"0.01\"\n  month:\n    max_cost_usd: \"1\"\n")
    seen = {}

    async def fake(model_id, prompt):
        seen["bound"] = budgets.output_bound(model_id, prompt)
        return "ok", {"output_tokens": 5, "cost_usd": 0.001, "model": model_id}

    monkeypatch.setattr(judge, "_ask_litellm", fake)
    assert asyncio.run(judge.ask_model("gpt-4o-mini", "Score this.", hub_dir=hub_dir)) == "ok"
    assert 0 < seen["bound"] < 20000                              # $0.01 of gpt-4o-mini output
    assert budgets.status(hub_dir)["open_reservations"] == []     # recorded and settled
    spend(hub_dir, cost=2.0)
    with pytest.raises(WorkItemError):
        asyncio.run(judge.ask_model("gpt-4o-mini", "Score this.", hub_dir=hub_dir))
