"""End-to-end eval run with a REAL LLM (claude-local).

Proves the chain the unit tests stub: `evals/*.md` on disk -> runtime.build ->
the agent actually answers and actually calls a tool -> the free assertions
see the real answer and the real tool calls -> a judged case gets a real score
from a real grading call -> the JSON record -> a real regression detected by
`--compare` after the hub's instructions are edited -> a scheduled case fired
by the real scheduler tick.

How to run:
    pytest tests/e2e/test_evals_e2e.py -m e2e -v

Self-skips when the `claude` CLI isn't installed / logged in. A handful of
short agent turns on claude-local/haiku — negligible subscription cost.
"""
from __future__ import annotations

import asyncio
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hubzoid import cli
from hubzoid.scheduling import ScheduleState

pytestmark = pytest.mark.e2e

runner = CliRunner()


def _claude_ready() -> bool:
    return shutil.which("claude") is not None


AGENTS = """\
---
name: E2E Eval Hub
description: eval e2e
---

You are a precise assistant for testing.

Behaviour rules:
- When asked for the refund window, answer exactly: the refund window is 14 days.
- Never invent an exception process. If asked about exceptions, say there are none.
- Keep answers to one short sentence.
"""


def _hub(tmp_path: Path, agents: str = AGENTS) -> Path:
    hub = tmp_path / "hub"
    (hub / "evals").mkdir(parents=True)
    hub.joinpath("AGENTS.md").write_text(agents)
    hub.joinpath(".env").write_text("MODEL=claude-local/haiku\n")
    return hub


def _case(hub: Path, name: str, text: str) -> None:
    (hub / "evals" / f"{name}.md").write_text(text, encoding="utf-8")


@pytest.mark.skipif(not _claude_ready(), reason="claude CLI not installed")
def test_free_tier_against_a_real_agent(tmp_path):
    """Step 1 of the walkthrough: the free checks, no judge, no cost."""
    hub = _hub(tmp_path)
    _case(hub, "refund-window",
          '---\ncontains: ["14 days"]\nnot_contains: ["as an AI"]\ntimeout: 120\n---\n'
          "What is the refund window for a cancelled program?\n")
    _case(hub, "no-invented-exception",
          '---\ncontains: ["no"]\n---\n'
          "Is there an exception process to get a refund after the window?\n")

    result = runner.invoke(cli.app, ["eval", "run", str(hub), "--no-judge"])
    assert result.exit_code == 0, result.stdout
    assert "2 passed" in result.stdout

    from hubzoid.evals import report as report_lib
    suite = report_lib.latest(hub)
    assert suite is not None and suite.passed == 2
    # A real model answered, not a stub. (Cases run sorted by name, so look
    # the one up rather than indexing.)
    refund = next(c for c in suite.cases if c.name == "refund-window")
    assert "14 days" in refund.response.lower()


@pytest.mark.skipif(not _claude_ready(), reason="claude CLI not installed")
def test_expect_tools_sees_a_real_tool_call(tmp_path):
    """The recorder must catch a call made by the actual Claude backend."""
    hub = _hub(tmp_path)
    (hub / "knowledge").mkdir()
    (hub / "knowledge" / "refund.md").write_text(
        "---\nname: refund_policy\ndescription: The refund policy.\n---\n"
        "The refund window is 14 days from cancellation.\n"
    )
    _case(hub, "cites-knowledge",
          "---\nexpect_tools: [read_knowledge]\n---\n"
          "Look up the refund policy in your knowledge files and quote it.\n")

    result = runner.invoke(cli.app, ["eval", "run", str(hub), "--no-judge"])
    assert result.exit_code == 0, result.stdout

    from hubzoid.evals import report as report_lib
    calls = report_lib.latest(hub).cases[0].tool_calls
    assert "read_knowledge" in calls, f"tools actually called: {calls}"


@pytest.mark.skipif(not _claude_ready(), reason="claude CLI not installed")
def test_a_real_judge_scores_against_agents_md(tmp_path):
    """Step 3: a real grading call, graded against the hub's own rules."""
    hub = _hub(tmp_path)
    _case(hub, "judged-refund",
          "---\nthreshold: 7\n---\n"
          "## Prompt\nWhat is the refund window for a cancelled program?\n\n"
          "## Criteria\nStates 14 days. Does not invent an exception process.\n")

    result = runner.invoke(cli.app, ["eval", "run", str(hub)])
    assert result.exit_code == 0, result.stdout

    from hubzoid.evals import report as report_lib
    judge = report_lib.latest(hub).cases[0].judge
    assert judge is not None and judge.error is None, judge
    assert judge.score >= 7, judge.reasoning


@pytest.mark.skipif(not _claude_ready(), reason="claude CLI not installed")
def test_editing_agents_md_is_caught_as_a_regression(tmp_path):
    """Step 2: break a behaviour rule on purpose, confirm --compare flags it.

    This is the property the whole feature exists for — a change to the hub's
    markdown showing up as a named regression rather than a user complaint.
    """
    hub = _hub(tmp_path)
    _case(hub, "refund-window", '---\ncontains: ["14 days"]\n---\n'
          "What is the refund window for a cancelled program?\n")

    first = runner.invoke(cli.app, ["eval", "run", str(hub), "--no-judge"])
    assert first.exit_code == 0, first.stdout

    # Break exactly one rule in the hub's instructions.
    hub.joinpath("AGENTS.md").write_text(
        AGENTS.replace("the refund window is 14 days", "the refund window is 30 days"))

    second = runner.invoke(cli.app, ["eval", "run", str(hub), "--no-judge", "--compare"])
    assert second.exit_code == 1, second.stdout
    assert "REGRESSIONS" in second.stdout
    assert "refund-window" in second.stdout


@pytest.mark.skipif(not _claude_ready(), reason="claude CLI not installed")
def test_scheduled_case_fires_through_the_real_scheduler(tmp_path):
    """Step 5: a `schedule:` case fired by the actual tick loop, end to end.

    Nothing is stubbed here — the scheduler computes due-ness, takes the lock,
    dispatches to the eval runner, which builds a real runtime and calls a real
    model. Only the clock is supplied.
    """
    from hubzoid import scheduler as scheduler_lib

    hub = _hub(tmp_path)
    _case(hub, "scheduled-refund",
          '---\nschedule: "* * * * *"\ncontains: ["14 days"]\n---\n'
          "What is the refund window for a cancelled program?\n")

    sched = scheduler_lib.Scheduler(hub, is_busy=lambda: False)
    now = datetime.now()

    async def go():
        await sched.check_once(now)                      # anchors, fires nothing
        return await sched.check_once(now + timedelta(minutes=5))

    fired = asyncio.run(go())
    assert fired == ["eval:scheduled-refund"], fired

    from hubzoid.evals import report as report_lib
    suite = report_lib.latest(hub)
    assert suite is not None and suite.ok, suite.cases[0].reason if suite.cases else "no cases"
    assert ScheduleState(hub).get("eval:scheduled-refund")["last_result"] == "pass"


@pytest.mark.skipif(not _claude_ready(), reason="claude CLI not installed")
def test_everything_works_with_no_langfuse_configured(tmp_path, monkeypatch):
    """Step 6: unset the endpoint; the local surfaces must be complete."""
    monkeypatch.delenv("HUBZOID_OTEL_ENDPOINT", raising=False)
    hub = _hub(tmp_path)
    _case(hub, "refund-window", '---\ncontains: ["14 days"]\n---\n'
          "What is the refund window for a cancelled program?\n")

    assert runner.invoke(cli.app, ["eval", "run", str(hub), "--no-judge"]).exit_code == 0
    status = runner.invoke(cli.app, ["eval", "status", str(hub)])
    assert "all passing" in status.stdout

    explain = runner.invoke(cli.app, ["eval", "explain", str(hub), "refund-window"])
    assert "14 days" in explain.stdout
    assert "AGENTS.md" in explain.stdout
