"""Scheduled evals — a case with `schedule:` fires itself inside `hubzoid run`.

This adds no second scheduler. The existing tick loop (`hubzoid/scheduler.py`)
already parses cron, computes anchor-based due-ness with missed-run catch-up,
gates on hub idleness, and holds a cross-process run lock. Eval cases reuse all
of it; the only new thing is a fork at fire time — a due file from `schedule/`
goes to the agent runner, a due file from `evals/` comes here.

Why the fork matters: `schedule_runner` drives an *agent* through rounds until
it says `STATUS: DONE`. Running evals that way would put a model in charge of
deciding whether the evals passed. Evals are deterministic; they get a
deterministic runner.

**Batching.** Every case due on this tick runs as one suite, not one run each.
Building the runtime is the expensive part (MCP init), so five cases sharing a
weekly cron cost one startup rather than five. Anchors are still tracked per
case, so a newly added case catches up on its own schedule.

**Naming.** State keys are namespaced `eval:<case>` so an eval case and a
scheduled task may share a name without fighting over the same anchor.

**One results file for every trigger.** `run_due` runs the suite through
`suite.arun_cases` (what `run_and_save` uses), so a scheduled run writes the
same schema 2 file as the CLI and the Console, with `trigger: "schedule"`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..scheduling import CronExpr, ScheduleState, is_due
from . import cases as cases_lib
from .cases import EvalCase

log = logging.getLogger("hubzoid.evals")

STATE_PREFIX = "eval:"


@dataclass(frozen=True)
class _Schedulable:
    """Minimal stand-in for a ScheduledTask.

    `is_due` / `next_fire_for` only ever touch `.name` and `.cron`, so this is
    all they need — and passing a namespaced name is what keeps eval anchors
    out of the scheduled-task anchors.
    """
    name: str
    cron: CronExpr


def state_key(case: EvalCase) -> str:
    return f"{STATE_PREFIX}{case.name}"


def _schedulable(case: EvalCase) -> _Schedulable:
    return _Schedulable(name=state_key(case), cron=case.cron)


def scheduled_cases(hub_dir: Path) -> list[EvalCase]:
    """Every enabled case carrying a `schedule:`.

    Non-strict discovery on purpose: one unparseable case file must not stop
    the others from firing. The bad file is logged and skipped, and the next
    manual `hubzoid eval run` reports it loudly.
    """
    return [c for c in cases_lib.discover(hub_dir, strict=False) if c.is_scheduled]


def due_cases(hub_dir: Path, state: ScheduleState,
              now: datetime | None = None) -> list[EvalCase]:
    """Scheduled cases whose next fire has passed. Stamps first_seen anchors."""
    now = now or datetime.now()
    return [c for c in scheduled_cases(hub_dir) if is_due(_schedulable(c), state, now)]


def next_fire_for(case: EvalCase, state: ScheduleState,
                  now: datetime | None = None):
    from ..scheduling import next_fire_for as _next
    return _next(_schedulable(case), state, now)


async def run_due(hub_dir: Path, due: list[EvalCase], state: ScheduleState,
                  *, now: datetime | None = None):
    """Run the due cases as one suite, persist, push, and record anchors.

    Returns the SuiteResult. Never raises for a failing case — a red suite is
    a result, not a crash. Anchors are recorded even when the suite fails, so
    a persistently failing case waits for its next cron match instead of
    re-firing on every tick.
    """
    from . import suite as suite_lib

    now = now or datetime.now()
    # The CLI's judge settings (HUBZOID_EVAL_JUDGE_MODEL, else the hub's
    # model), the same results file, and the Langfuse push.
    path, suite = await suite_lib.arun_cases(hub_dir, due, judge=True, trigger="schedule")

    for case in due:
        result = next((r for r in suite.cases if r.name == case.name), None)
        state.record_fired(
            state_key(case), now,
            result="pass" if (result and result.passed) else "fail",
            run_log=str(path),
        )

    # A scheduled eval nobody looks at is worse than no eval: it manufactures
    # confidence. Failures go out at ERROR so they surface in whatever the
    # operator already watches (journalctl, the log drain), not only in
    # `hubzoid eval status`.
    if suite.ok:
        log.info("evals: %d/%d passed", suite.passed, len(suite.cases))
    else:
        failing = ", ".join(f"{c.name} ({c.reason})" for c in suite.cases if not c.passed)
        log.error("evals: %d of %d FAILED — %s", suite.failed, len(suite.cases), failing)

    return suite
