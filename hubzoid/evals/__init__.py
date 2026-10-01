"""Hubzoid evals — "is this hub still doing what it is supposed to do?"

One markdown file per case in `<hub>/evals/`, run through the hub's own
runtime, checked first with model-free assertions and then (optionally) graded
by a model against the hub's own instructions.

    hubzoid eval <hub>                  # everything
    hubzoid eval <hub> --no-judge       # skip grading (the agent still runs)
    hubzoid eval <hub> --compare        # what regressed since last run

Layout:

    cases.py       the `evals/*.md` format: parse, discover, filter
    assertions.py  model-free checks (substrings, tool calls) over a finished run
    judge.py       the model tier — grades against AGENTS.md + `## Criteria`
    calls.py       tool call details: safe arguments, previews, one-line views
    runner.py      execution against `runtime.build(hub)` — the real hub
    suite.py       `run_and_save`: discover, run, judge, save (every trigger)
    report.py      terminal table, run JSON, `--compare`
    langfuse.py    optional push to Langfuse when OTel is configured

Every trigger, one runner and one results file: manual (`hubzoid eval`), cron
(`schedule:` in a case file, fired by the existing scheduler), the Console,
and CI (the exit code) all go through `run_and_save`.
"""
from __future__ import annotations

from .cases import EvalCase, EvalCaseError, discover, parse, select
from .report import compare, latest, load_runs, render_compare, render_table, save
from .results import CaseResult, JudgeResult, SuiteResult, ToolCallRecord, TurnRecord
from .runner import arun_suite, run_suite
from .suite import arun_and_save, run_and_save

__all__ = [
    "EvalCase", "EvalCaseError", "discover", "parse", "select",
    "CaseResult", "JudgeResult", "SuiteResult", "ToolCallRecord", "TurnRecord",
    "arun_suite", "run_suite", "run_and_save", "arun_and_save",
    "compare", "latest", "load_runs", "render_compare", "render_table", "save",
]
