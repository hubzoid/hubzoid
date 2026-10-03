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

from . import calls as calls_lib
from .results import CaseResult, SuiteResult

log = logging.getLogger("hubzoid.evals")

RUNS_DIRNAME = ".hubzoid/evals"

# Keep the last N run files. Enough for a meaningful history on a box with no
# Langfuse; small enough that a hub folder never quietly grows without bound
# (responses are stored verbatim, so files are not tiny).
KEEP_RUNS = 200


def runs_dir(hub_dir: Path) -> Path:
    return Path(hub_dir) / RUNS_DIRNAME


from contextlib import contextmanager

@contextmanager
def _locked(d):
    import fcntl
    import os
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(d, 0o700)
    fd = os.open(d / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _atomic(path, data):
    import os
    import tempfile
    fd, tmp = tempfile.mkstemp(prefix=".eval-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _files(d):
    return sorted(p for p in d.glob("*.json") if p.name != "index.json" and not p.is_symlink())


def _entry(path, raw):
    suite = SuiteResult.from_dict(raw)
    return dict(stamp=path.stem, schema=raw.get("schema", 1), trigger=raw.get("trigger"),
                started=suite.started_at, finished=suite.finished_at, model=suite.model,
                judge_model=suite.judge_model, judged=suite.judged,
                passed=suite.passed, failed=suite.failed, total=len(suite.cases),
                cases=[dict(name=c.name, passed=c.passed) for c in suite.cases])


def _index(d):
    files = _files(d)
    path = d / "index.json"
    try:
        index = json.loads(path.read_text())
        if set(index) == {p.stem for p in files}:
            return index
    except (OSError, ValueError, TypeError):
        pass
    index = {}
    for path in files:
        try:
            index[path.stem] = _entry(path, json.loads(path.read_text()))
        except (OSError, ValueError, TypeError):
            log.warning("Skipping unreadable eval run %s", path.name)
    _atomic(d / "index.json", index)
    return index


def summaries(hub_dir, *, offset=0, limit=50):
    d = runs_dir(hub_dir)
    if not d.exists():
        return [], 0
    with _locked(d):
        index = _index(d)
        keys = sorted(index, reverse=True)
        return [index[k] for k in keys[offset:offset+limit]], len(keys)


def save(hub_dir: Path, suite: SuiteResult, *, stamp: str | None = None) -> Path:
    import os
    import re
    import uuid
    d = runs_dir(hub_dir)
    stamp = stamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,60}", stamp):
        raise ValueError("Invalid eval result stamp")
    path = d / f"{stamp}_{uuid.uuid4().hex[:12]}.json"
    with _locked(d):
        index = _index(d)
        raw = calls_lib.redact(suite.to_dict())
        _atomic(path, raw)
        index[path.stem] = _entry(path, raw)
        keep = int(os.environ.get("HUBZOID_EVAL_KEEP_RUNS", KEEP_RUNS))
        if keep < 0:
            raise ValueError("HUBZOID_EVAL_KEEP_RUNS must be nonnegative")
        if keep:
            for key in sorted(index)[:-keep]:
                (d / (key + ".json")).unlink(missing_ok=True)
                del index[key]
        _atomic(d / "index.json", index)
    return path


def _prune(d):
    # Compatibility helper for operator scripts; saving handles index atomically.
    import os
    with _locked(d):
        index = _index(d)
        keep = int(os.environ.get("HUBZOID_EVAL_KEEP_RUNS", KEEP_RUNS))
        if keep > 0:
            for key in sorted(index)[:-keep]:
                (d / (key + ".json")).unlink(missing_ok=True)
                del index[key]
        _atomic(d / "index.json", index)


def load_runs(hub_dir: Path, limit: int = 2) -> list[tuple[Path, SuiteResult]]:
    d = runs_dir(hub_dir)
    if not d.is_dir():
        return []
    out = []
    with _locked(d):
        for path in _files(d)[-limit:]:
            try:
                out.append((path, SuiteResult.from_dict(json.loads(path.read_text()))))
            except (OSError, ValueError, TypeError):
                log.warning("Skipping unreadable eval run %s", path.name)
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


def detail_lines(c: CaseResult) -> list[str]:
    """`--details`: who the case ran as, then one line per tool call, e.g.
    `read_knowledge name=refund-policy  ok  120 ms` (prefixed with its turn
    in a multi-turn case). Plain text: escape it before printing with markup."""
    lines: list[str] = []
    if c.run_as:
        lines.append(f"run as {c.run_as}")
    if not c.tools:
        lines.append("(no tool calls)")
    multi = c.turns is not None
    for call in c.tools:
        lines.append(calls_lib.describe(call, turn=multi))
    return lines


def render_table(console, suite: SuiteResult) -> None:
    """The primary surface for manual and CI runs."""
    from rich.markup import escape
    from rich.table import Table

    table = Table(box=None, pad_edge=False)
    table.add_column("case", style="cyan", no_wrap=True)
    table.add_column("", width=4)
    table.add_column("judge", width=9)
    table.add_column("time", justify="right", width=7)
    table.add_column("reason", style="dim", overflow="fold")

    for c in suite.cases:
        table.add_row(escape(c.name), _verdict_cell(c), _judge_cell(c),
                      f"{c.duration:.1f}s", escape(c.reason))
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
