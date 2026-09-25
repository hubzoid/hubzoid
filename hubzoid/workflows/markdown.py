# Hubzoid workflows. MIT licensed like the rest of the repository.
"""Markdown schedule tasks (`<hub>/schedule/*.md`) and scheduled evals, run on DBOS.

This module is only the executor. *When* a task is due is unchanged and lives in
`scheduling.py` / `scheduler.py`: cron in machine-local time, anchors in the
schedule-state file, one catch-up run after downtime, deferral while the hub is
serving chat. When a task is due, the scheduler enqueues one run here on the
hub's markdown queue (one markdown run at a time per hub, across processes), so
every run is durable and appears in the run history next to code workflows.

A run is split into checkpointed steps:

  1. work   - the agent rounds, or the `run:` command. Never repeated: if the
              process dies mid-step, the recovered run reports the interruption
              instead of doing the work again (the same outcome as before DBOS:
              a crashed run waits for its next slot).
  2. commit - the declared `commit:` paths only; skipped when nothing changed.
  3. push   - `pull --rebase` then push; safe to retry. A rebase conflict fails
              the run cleanly and leaves the commit local.
  4. finish - record the result; archive the webhook events the run handled.

All tasks share one registered DBOS workflow (`hz_markdown_task`); the run's
workflow id `md:<task>:<slot>` carries the task name, so tasks added or edited
while the hub runs work without re-registering, and the same slot can never be
enqueued twice.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path

log = logging.getLogger("hubzoid.workflows")

MD_WORKFLOW = "hz_markdown_task"
EVAL_WORKFLOW = "hz_eval_suite"
_FNS: dict = {}


def run_id(task_name: str, slot: str) -> str:
    return f"md:{task_name}:{slot}"


def task_name_from_id(workflow_id: str) -> str | None:
    """The markdown task a run belongs to, from its workflow id."""
    if workflow_id.startswith("md:"):
        return workflow_id[3:].rsplit(":", 1)[0] if workflow_id.count(":") >= 2 else workflow_id[3:]
    return None


def register(DBOS, hub_dir: Path, hub_name: str) -> None:
    """Register the markdown-task and eval-suite workflows (called by init)."""
    from .. import schedule_runner as runner
    from .. import scheduling as sch

    def _task(task_name: str, overrides: dict):
        tasks, _ = sch.load_tasks(hub_dir)
        task = next((t for t in tasks if t.name == task_name), None)
        if task is None:
            raise LookupError(f"schedule/{task_name}.md no longer exists")
        for key in ("timeout", "max_rounds", "model"):
            if overrides.get(key):
                setattr(task, key, overrides[key])
        return task

    @DBOS.step()
    def work(task_name: str, overrides: dict, run: str) -> dict:
        from .state import WorkflowState
        from .. import db

        marker = WorkflowState(db.operational_engine(hub_dir), hub_name, f"md:{task_name}")
        key = f"started:{run}"
        if key in marker:
            return {"result": "error", "rounds": 0, "summary": "",
                    "error": "interrupted by a restart; not re-run (the next slot runs it)",
                    "run_log": marker[key]}
        task = _task(task_name, overrides)
        marker[key] = "starting"
        result = asyncio.run(runner.run_task(hub_dir, task, capture=False))
        marker[key] = str(result.run_log)
        return {"result": result.result, "rounds": result.rounds, "summary": result.summary,
                "error": result.error, "run_log": str(result.run_log)}

    @DBOS.step()
    def commit(task_name: str, overrides: dict, summary: str, started: str) -> str | None:
        task = _task(task_name, overrides)
        return runner.commit_paths(hub_dir, task.commit, runner.commit_message(task, summary, started))

    @DBOS.step(retries_allowed=True, max_attempts=2)
    def push() -> None:
        runner.push_head(hub_dir)

    @DBOS.step()
    def finish(task_name: str, outcome: dict, claimed: list[str], started: str) -> None:
        sch.ScheduleState(hub_dir).record_fired(
            task_name, datetime.fromisoformat(started), result=outcome["result"],
            run_log=outcome.get("run_log"))
        if claimed and outcome["result"] == "done":
            from ..inbound.webhook import archive_events

            archive_events([Path(p) for p in claimed if Path(p).exists()])

    @DBOS.workflow(name=MD_WORKFLOW)
    def md_task(task_name: str, claimed: list[str], overrides: dict) -> dict:
        run = DBOS.workflow_id
        started = datetime.now().isoformat(timespec="seconds")
        outcome = work(task_name, overrides, run)
        if outcome["result"] == "done":
            task = _task(task_name, overrides)
            if task.commit:
                try:
                    sha = commit(task_name, overrides, outcome["summary"], started)
                    outcome["commit_sha"] = sha
                    if task.push:
                        push()
                        outcome["pushed"] = True
                except Exception as exc:  # noqa: BLE001 — a git failure fails the run
                    outcome["result"] = "error"
                    outcome["error"] = f"{type(exc).__name__}: {exc}"
        finish(task_name, outcome, claimed, started)
        if outcome["result"] == "error":
            raise RuntimeError(outcome.get("error") or "scheduled task failed")
        return outcome

    @DBOS.step()
    def run_evals(names: list[str], now_iso: str) -> dict:
        from ..evals import schedule as evals_schedule

        cases = [c for c in evals_schedule.scheduled_cases(hub_dir) if c.name in names]
        if not cases:
            return {"cases": 0}
        state = sch.ScheduleState(hub_dir)
        suite = asyncio.run(evals_schedule.run_due(hub_dir, cases, state,
                                                   now=datetime.fromisoformat(now_iso)))
        return {"cases": len(cases), "failed": [c.name for c in suite.cases if not c.passed]}

    @DBOS.workflow(name=EVAL_WORKFLOW)
    def eval_suite(names: list[str], now_iso: str) -> dict:
        return run_evals(names, now_iso)

    _FNS["md_task"] = md_task
    _FNS["eval_suite"] = eval_suite


def enqueue_task(task_name: str, slot: str, claimed: list[str] | None = None,
                 overrides: dict | None = None):
    """Queue one run of a markdown task. The same (task, slot) is only ever queued
    once. Returns the DBOS handle."""
    from dbos import SetWorkflowID

    from . import runtime

    with SetWorkflowID(run_id(task_name, slot)):
        return runtime._MD_QUEUE.enqueue(_FNS["md_task"], task_name,
                                         list(claimed or []), dict(overrides or {}))


def enqueue_evals(names: list[str], now: datetime):
    from dbos import SetWorkflowID

    from . import runtime

    slot = now.strftime("%Y%m%dT%H%M")
    with SetWorkflowID(f"eval:{','.join(sorted(names))}:{slot}"):
        return runtime._MD_QUEUE.enqueue(_FNS["eval_suite"], list(names), now.isoformat())
