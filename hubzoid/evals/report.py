"""Local surfaces: the terminal table, the JSON record, and `--compare`.

None of this needs Langfuse, a database, or a network. That is deliberate —
evals have to work on a laptop and on an air-gapped customer box with no
infrastructure at all, so the local files are the floor and Langfuse is an
upgrade layered on top (see `langfuse.py`).

Regression detection is a diff of the last two JSON files. It never needed a
database, which is exactly why this works with nothing installed.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .results import CaseResult, SuiteResult

log = logging.getLogger("hubzoid.evals")

RUNS_DIRNAME = ".hubzoid/evals"

# Keep the last N run files. Enough for a meaningful history on a box with no
# Langfuse; small enough that a hub folder never quietly grows without bound
# (responses are stored verbatim, so files are not tiny).
KEEP_RUNS = 50


def runs_dir(hub_dir: Path) -> Path:
    return hub_dir / RUNS_DIRNAME


def save(hub_dir: Path, suite: SuiteResult, *, stamp: str | None = None) -> Path:
    """Write the run JSON and prune old ones. Returns the path written."""
    d = runs_dir(hub_dir)
    d.mkdir(parents=True, exist_ok=True)
    stamp = stamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    path = d / f"{stamp}.json"
    path.write_text(json.dumps(suite.to_dict(), indent=2), encoding="utf-8")
    _prune(d)
    return path


def _prune(d: Path) -> None:
    runs = sorted(d.glob("*.json"))
    for old in runs[:-KEEP_RUNS]:
        try:
            old.unlink()
        except OSError:  # noqa: PERF203 — pruning is best-effort
            log.debug("could not prune old eval run %s", old.name)


def load_runs(hub_dir: Path, limit: int = 2) -> list[tuple[Path, SuiteResult]]:
    """The most recent runs, newest last. Unreadable files are skipped."""
    d = runs_dir(hub_dir)
    if not d.is_dir():
        return []
    out: list[tuple[Path, SuiteResult]] = []
    for path in sorted(d.glob("*.json"))[-limit:]:
        try:
            out.append((path, SuiteResult.from_dict(json.loads(path.read_text(encoding="utf-8")))))
        except (OSError, ValueError, json.JSONDecodeError):
            log.warning("skipping unreadable eval run %s", path.name)
    return out


def latest(hub_dir: Path) -> SuiteResult | None:
    runs = load_runs(hub_dir, limit=1)
    return runs[-1][1] if runs else None


# --------------------------------------------------------------------------
# Compare
# --------------------------------------------------------------------------
@dataclass
class Delta:
    name: str
    kind: str          # "regression" | "fixed" | "added" | "removed"
    detail: str = ""

    @property
    def label(self) -> str:
        return {
            "regression": "PASS → FAIL",
            "fixed": "FAIL → PASS",
            "added": "new",
            "removed": "gone",
        }[self.kind]


def compare(prev: SuiteResult, cur: SuiteResult) -> list[Delta]:
    """What moved between two runs. Only changes — an all-green diff is empty.

    Cases are matched by name, so renaming a case file reads as one removed
    and one added rather than a phantom regression.
    """
    before = {c.name: c for c in prev.cases}
    after = {c.name: c for c in cur.cases}
    deltas: list[Delta] = []

    for name, now in after.items():
        was = before.get(name)
        if was is None:
            deltas.append(Delta(name, "added", now.reason if not now.passed else ""))
        elif was.passed and not now.passed:
            deltas.append(Delta(name, "regression", now.reason))
        elif not was.passed and now.passed:
            deltas.append(Delta(name, "fixed"))

    for name in before:
        if name not in after:
            deltas.append(Delta(name, "removed"))

    order = {"regression": 0, "removed": 1, "added": 2, "fixed": 3}
    return sorted(deltas, key=lambda d: (order[d.kind], d.name))


# --------------------------------------------------------------------------
# Terminal rendering
# --------------------------------------------------------------------------
def _verdict_cell(c: CaseResult) -> str:
    return "[green]PASS[/green]" if c.passed else "[red]FAIL[/red]"


def _judge_cell(c: CaseResult) -> str:
    if c.judge is None:
        return "[dim]—[/dim]"
    if c.judge.error:
        return "[yellow]judge err[/yellow]"
    colour = "green" if c.judge.passed else "red"
    return f"[{colour}]{c.judge.score}/10[/{colour}]"


def render_table(console, suite: SuiteResult) -> None:
    """The primary surface for manual and CI runs."""
    from rich.table import Table

    table = Table(box=None, pad_edge=False)
    table.add_column("case", style="cyan", no_wrap=True)
    table.add_column("", width=4)
    table.add_column("judge", width=9)
    table.add_column("time", justify="right", width=7)
    table.add_column("reason", style="dim", overflow="fold")

    for c in suite.cases:
        table.add_row(c.name, _verdict_cell(c), _judge_cell(c),
                      f"{c.duration:.1f}s", c.reason)
    console.print(table)

    total = len(suite.cases)
    if suite.failed:
        console.print(f"\n[red]{suite.failed} failed[/red], {suite.passed} passed "
                      f"of {total}")
    else:
        console.print(f"\n[green]{suite.passed} passed[/green] of {total}")


def render_compare(console, deltas: list[Delta], *, prev_name: str) -> None:
    if not deltas:
        console.print(f"[green]No change[/green] since {prev_name}.")
        return
    regressions = [d for d in deltas if d.kind == "regression"]
    if regressions:
        console.print(f"[red]REGRESSIONS: {len(regressions)}[/red]")
    for d in deltas:
        colour = {"regression": "red", "fixed": "green",
                  "added": "cyan", "removed": "yellow"}[d.kind]
        detail = f"  {d.detail}" if d.detail else ""
        console.print(f"  [{colour}]{d.name:<28}{d.label}[/{colour}]{detail}")
