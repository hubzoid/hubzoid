"""End-to-end scheduled-task run with a REAL LLM (claude-local).

Proves the whole chain the unit tests stub: runtime.build with the injected
schedule tools → the agent actually calls run_git to read a real commit →
writes a knowledge doc through write_hub_file → emits the STATUS line → the
runner captures a scoped git commit.

How to run:
    pytest tests/e2e/test_schedule_e2e.py -m e2e -v

Self-skips when the `claude` CLI isn't installed / logged in. One short
agent session on claude-local/haiku — negligible subscription cost.
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e


def _run(args, cwd):
    subprocess.run(args, cwd=str(cwd), check=True, capture_output=True, text=True)


def _claude_ready() -> bool:
    return shutil.which("claude") is not None


@pytest.mark.skipif(not _claude_ready(), reason="claude CLI not installed")
def test_scheduled_task_end_to_end(tmp_path):
    # --- a tiny hub, itself a git repo so the commit capture runs too ---
    hub = tmp_path / "hub"
    (hub / "knowledge").mkdir(parents=True)
    (hub / "knowledge" / ".gitkeep").write_text("")
    (hub / "AGENTS.md").write_text(
        "---\nname: E2E Hub\ndescription: schedule e2e\n---\n\n"
        "You are a precise background maintenance agent.\n"
    )
    (hub / ".env").write_text("MODEL=claude-local/haiku\n")
    (hub / ".gitignore").write_text("raw_data/\n.hubzoid/\n.env\n")
    _run(["git", "init", "-q"], hub)
    _run(["git", "config", "user.email", "e2e@t.dev"], hub)
    _run(["git", "config", "user.name", "e2e"], hub)
    _run(["git", "add", "-A"], hub)
    _run(["git", "commit", "-q", "-m", "init"], hub)

    # --- a source repo with one distinctive commit subject ---
    repo = hub / "raw_data" / "core"
    repo.mkdir(parents=True)
    _run(["git", "init", "-q"], repo)
    _run(["git", "config", "user.email", "e2e@t.dev"], repo)
    _run(["git", "config", "user.name", "e2e"], repo)
    (repo / "billing.py").write_text("TAX = 0.18\n")
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-q", "-m", "add zebra-billing module"], repo)

    # --- the task, exactly as a hub author would write it ---
    from hubzoid import schedule_runner as runner
    from hubzoid import scheduling as sch

    (hub / "schedule").mkdir()
    (hub / "schedule" / "log-commits.md").write_text(
        '---\nschedule: "0 3 * * *"\ncommit: ["knowledge/"]\n'
        "timeout: 240\nmax_rounds: 2\n---\n\n"
        "Use run_git on raw_data/core to find the subject line of its most "
        "recent commit. Then use write_hub_file to create "
        "knowledge/commit-log.md containing exactly that subject line. "
        "That is the whole task.\n"
    )
    [task], problems = sch.load_tasks(hub)
    assert problems == []

    result = asyncio.run(runner.run_task(hub, task))

    assert result.ok, f"run failed: {result.result} {result.error} (log: {result.run_log})"
    doc = hub / "knowledge" / "commit-log.md"
    assert doc.is_file(), "agent did not write the knowledge doc"
    assert "zebra-billing" in doc.read_text(), doc.read_text()
    # the runner (not the agent) committed it, scoped to knowledge/
    msg = subprocess.run(["git", "-C", str(hub), "log", "-1", "--format=%s"],
                         capture_output=True, text=True, check=True).stdout
    assert msg.startswith("schedule(log-commits):")
