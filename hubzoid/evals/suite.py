"""Run a hub's cases and save the results file: one path for every trigger.

`run_and_save` is what the CLI (`hubzoid eval run`), scheduled evals
(`schedule.run_due`) and the Console use, so each trigger produces the same
results file (`<hub>/.hubzoid/evals/<stamp>.json`, schema 2) with its
`trigger` recorded. It composes the pieces that stay separate on purpose:
discovery (`cases`), the model-free runner (`runner`), the judge (`judge`,
built only when a selected case has criteria), the JSON record (`report`) and
the optional Langfuse push.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Callable, Iterable

from .cases import EvalCase, EvalCaseError
from .results import TRIGGERS, CaseResult, SuiteResult

log = logging.getLogger("hubzoid.evals")


def select_cases(hub_dir: Path, names: list[str] | None = None) -> list[EvalCase]:
    """All enabled cases (`names` None), else exactly those names, in the
    order given. Strict discovery: a bad case file raises EvalCaseError, as
    does an unknown name or an empty selection."""
    from . import cases as cases_lib

    found = cases_lib.discover(Path(hub_dir))
    if names is None:
        chosen = [c for c in found if c.enabled]
        if not chosen:
            raise EvalCaseError(f"no enabled eval cases in {Path(hub_dir).name}/evals/")
        return chosen
    by_name = {c.name: c for c in found}
    wanted = list(dict.fromkeys(names))
    unknown = [n for n in wanted if n not in by_name]
    if unknown:
        known = ", ".join(sorted(by_name)) or "(none)"
        raise EvalCaseError(f"unknown eval case(s): {', '.join(unknown)}. Known: {known}")
    if not wanted:
        raise EvalCaseError("no eval cases named")
    return [by_name[n] for n in wanted]


def check_run_as(run_as: str | None) -> str | None:
    """Syntax check of a suite default account (operator input). The account
    itself is resolved per case, when it runs."""
    if run_as is None or not str(run_as).strip():
        return None
    from ..workflows.identity import validate_run_as

    try:
        return validate_run_as(run_as)
    except ValueError as exc:
        raise EvalCaseError(str(exc)) from exc


def _check_trigger(trigger: str) -> str:
    if trigger not in TRIGGERS:
        raise ValueError(f"trigger must be one of {', '.join(TRIGGERS)}, got {trigger!r}")
    return trigger


async def arun_cases(hub_dir: Path, cases: Iterable[EvalCase], *, judge: bool = True,
                     run_as: str | None = None, trigger: str = "cli",
                     model: str | None = None, judge_model: str | None = None,
                     on_case: Callable[[CaseResult], None] | None = None,
                     push: bool = True) -> tuple[Path, SuiteResult]:
    """Run already selected cases, save the results file, push to Langfuse
    (best effort, when `push`), and return (path, suite)."""
    from . import judge as judge_lib
    from . import report as report_lib
    from . import runner as runner_lib

    hub_dir = Path(hub_dir).resolve()
    cases = list(cases)
    _check_trigger(trigger)
    run_as = check_run_as(run_as)

    judge_fn = None
    if judge and any(c.is_judged for c in cases):
        judge_fn = judge_lib.make_judge(hub_dir, model=judge_model)

    suite = await runner_lib.arun_suite(hub_dir, cases, judge_fn=judge_fn, on_case=on_case,
                                        model=model, run_as=run_as, trigger=trigger)
    if judge_fn is not None:
        suite.judge_model = getattr(judge_fn, "model_id", None)
    path = report_lib.save(hub_dir, suite)
    if push:
        push_to_langfuse(hub_dir, suite)
    return path, suite


def push_to_langfuse(hub_dir: Path, suite: SuiteResult) -> str | None:
    """Best effort. Never fails a run: the local JSON is the record."""
    from . import langfuse as langfuse_lib

    try:
        pushed = langfuse_lib.push(hub_dir, suite)
    except Exception as exc:  # noqa: BLE001 — never let a telemetry outage matter
        log.warning("evals: langfuse push skipped: %s", exc)
        return None
    if pushed:
        log.info("evals: pushed to langfuse — %s", pushed)
    return pushed


async def arun_and_save(hub_dir: Path, names: list[str] | None = None, *, judge: bool = True,
                        run_as: str | None = None, trigger: str = "cli",
                        model: str | None = None, **kwargs) -> tuple[Path, SuiteResult]:
    """`run_and_save` for a caller already on an event loop."""
    cases = select_cases(Path(hub_dir).resolve(), names)
    return await arun_cases(hub_dir, cases, judge=judge, run_as=run_as, trigger=trigger,
                            model=model, **kwargs)


def run_and_save(hub_dir: Path, names: list[str] | None = None, *, judge: bool = True,
                 run_as: str | None = None, trigger: str = "cli",
                 model: str | None = None, judge_model: str | None = None,
                 on_case: Callable[[CaseResult], None] | None = None,
                 push: bool = True) -> tuple[Path, SuiteResult]:
    """Discover the hub's cases (all enabled cases when names is None, else those
    names), run them with the CLI's judge settings, save the results file and
    return (path, suite). Raises EvalCaseError for unknown names or bad case
    files. Blocking; call it from a worker thread or a DBOS step, never on the
    event loop.

    `judge=False` skips grading (the agent still runs). `run_as` is the
    account cases without their own `run_as` run as (operator input only).
    `trigger` (cli, schedule, console, ci) is recorded in the file.
    `judge_model` pins the grader (else HUBZOID_EVAL_JUDGE_MODEL, else the
    hub's model), `on_case` sees each result as it finishes, and `push=False`
    leaves the Langfuse push to the caller.
    """
    hub_dir = Path(hub_dir).resolve()
    cases = select_cases(hub_dir, names)
    run_as = check_run_as(run_as)
    _check_trigger(trigger)
    return asyncio.run(arun_cases(hub_dir, cases, judge=judge, run_as=run_as,
                                  trigger=trigger, model=model, judge_model=judge_model,
                                  on_case=on_case, push=push))
