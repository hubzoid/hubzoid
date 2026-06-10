"""Scheduled tasks — declaration + state for `<hub>/schedule/*.md`.

A hub declares background jobs as markdown files, exactly like `knowledge/`
and `skills/`: frontmatter says *when* (5-field cron) and *what the run may
touch* (commit paths); the body is the agent's instructions in plain English.
Hubzoid owns the mechanism (this module + `scheduler.py` + `schedule_runner.py`);
the hub owns the policy (the md files).

Three layers, mirroring Claude Code's cron system:

  * **declaration / state (this module)** — parse cron expressions, discover
    and validate `schedule/*.md`, persist per-task fire anchors in
    `<hub>/.hubzoid/schedule-state.json`, and a coarse cross-process run lock.
  * **scheduler (`scheduler.py`)** — the in-process tick loop that decides
    *when* to fire (idle-gated, missed-run catch-up).
  * **execution (`schedule_runner.py`)** — the round harness that runs a
    task's instructions through the hub's own Runtime until done.

Due-ness model (same trick as Claude Code's `lastFiredAt ?? createdAt`):
each task's next fire is computed from its **anchor** — `last_fired_at` if it
has ever fired, else `first_seen_at` (stamped when the task file is first
discovered). A task is due when that next-fire time is in the past. This gives
missed-run catch-up for free: if the process was down across a scheduled
match, the anchor is old, the next fire computes in the past, and the task
fires once on the next tick — never N times for N missed matches.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import frontmatter
from ._fs import resolve_bucket

log = logging.getLogger("hubzoid.schedule")

STATE_DIRNAME = ".hubzoid"
STATE_FILENAME = "schedule-state.json"
LOCK_FILENAME = "schedule.lock"

# Frontmatter defaults. Kept here so the CLI, docs and tests agree.
DEFAULT_TIMEOUT = 1800      # seconds per round
DEFAULT_MAX_ROUNDS = 10     # fresh-context rounds per run
DEFAULT_MAX_TURNS = 40      # agent turns within one round


# ---------------------------------------------------------------------------
# Cron: 5-field expressions, local time.
# ---------------------------------------------------------------------------
# Supported syntax per field: "*", "*/N", "A", "A-B", "A-B/N", comma lists.
# Numeric only (no JAN/MON names). Day-of-week: 0=Sunday..6=Saturday, 7=Sunday.
# Standard cron quirk preserved: when BOTH day-of-month and day-of-week are
# restricted, a day matches if EITHER matches (OR); otherwise both must (AND
# with the unrestricted one trivially true).

_FIELD_BOUNDS = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day-of-month", 1, 31),
    ("month", 1, 12),
    ("day-of-week", 0, 7),
)


@dataclass(frozen=True)
class CronExpr:
    raw: str
    minutes: frozenset[int]
    hours: frozenset[int]
    doms: frozenset[int]
    months: frozenset[int]
    dows: frozenset[int]          # normalized: 0=Sunday..6=Saturday
    dom_star: bool
    dow_star: bool


def _parse_field(spec: str, name: str, lo: int, hi: int) -> tuple[frozenset[int], bool]:
    """Parse one cron field into (allowed values, was-a-plain-star)."""
    values: set[int] = set()
    is_star = spec.strip() == "*"
    for part in spec.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"{name}: empty list element in {spec!r}")
        step = 1
        if "/" in part:
            rng, _, step_s = part.partition("/")
            if not step_s.isdigit() or int(step_s) < 1:
                raise ValueError(f"{name}: bad step in {part!r}")
            step = int(step_s)
        else:
            rng = part
        if rng == "*":
            start, end = lo, hi
        elif "-" in rng:
            a, _, b = rng.partition("-")
            if not (a.strip().lstrip("-").isdigit() and b.strip().isdigit()):
                raise ValueError(f"{name}: bad range {part!r}")
            start, end = int(a), int(b)
        else:
            if not rng.strip().lstrip("-").isdigit():
                raise ValueError(f"{name}: bad value {part!r}")
            start = end = int(rng)
        if start < lo or end > hi or start > end:
            raise ValueError(f"{name}: {part!r} out of range {lo}-{hi}")
        values.update(range(start, end + 1, step))
    if name == "day-of-week" and 7 in values:   # 7 is Sunday, same as 0
        values.discard(7)
        values.add(0)
    return frozenset(values), is_star


def parse_cron(expr: str) -> CronExpr:
    """Parse a standard 5-field cron expression. Raises ValueError on junk."""
    fields = str(expr).split()
    if len(fields) != 5:
        raise ValueError(
            f"cron expression must have 5 fields (minute hour day-of-month "
            f"month day-of-week), got {len(fields)}: {expr!r}"
        )
    parsed: list[tuple[frozenset[int], bool]] = [
        _parse_field(f, name, lo, hi)
        for f, (name, lo, hi) in zip(fields, _FIELD_BOUNDS)
    ]
    return CronExpr(
        raw=str(expr).strip(),
        minutes=parsed[0][0],
        hours=parsed[1][0],
        doms=parsed[2][0],
        months=parsed[3][0],
        dows=parsed[4][0],
        dom_star=parsed[2][1],
        dow_star=parsed[4][1],
    )


def _day_matches(c: CronExpr, d: datetime) -> bool:
    if d.month not in c.months:
        return False
    dom_ok = d.day in c.doms
    dow_ok = ((d.weekday() + 1) % 7) in c.dows   # python Mon=0 -> cron Sun=0
    if c.dom_star and c.dow_star:
        return True
    if c.dom_star:
        return dow_ok
    if c.dow_star:
        return dom_ok
    return dom_ok or dow_ok                       # both restricted: cron ORs them


def next_fire(c: CronExpr, after: datetime) -> datetime | None:
    """First matching wall-clock minute strictly after `after` (local, naive).

    Scans day-by-day (cheap: day match is checked once per day, then only the
    allowed hours/minutes are enumerated) up to ~2 years out, which covers
    every satisfiable 5-field expression; returns None for the unsatisfiable
    leftovers (e.g. Feb 30).
    """
    t = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    hours = sorted(c.hours)
    minutes = sorted(c.minutes)
    for _ in range(366 * 2 + 1):
        if _day_matches(c, t):
            for h in hours:
                if h < t.hour:
                    continue
                for m in minutes:
                    if h == t.hour and m < t.minute:
                        continue
                    return t.replace(hour=h, minute=m)
        t = (t + timedelta(days=1)).replace(hour=0, minute=0)
    return None


def cron_to_human(c: CronExpr) -> str:
    """Tiny best-effort humanization for `schedule list` output."""
    raw = c.raw
    m, h, dom, mon, dow = raw.split()
    days = {0: "Sun", 1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat"}
    if dom == "*" and mon == "*" and dow == "*":
        if h == "*" and m.startswith("*/"):
            return f"every {m[2:]} min"
        if h == "*" and m.isdigit():
            return f"hourly at :{int(m):02d}"
        if h.isdigit() and m.isdigit():
            return f"daily at {int(h):02d}:{int(m):02d}"
    if dom == "*" and mon == "*" and dow.isdigit() and h.isdigit() and m.isdigit():
        return f"{days.get(int(dow) % 7, dow)} at {int(h):02d}:{int(m):02d}"
    return raw


# ---------------------------------------------------------------------------
# Task declaration: <hub>/schedule/*.md
# ---------------------------------------------------------------------------
@dataclass
class ScheduledTask:
    name: str                                   # filename stem, the task id
    schedule: str                               # raw cron string
    cron: CronExpr
    body: str                                   # the agent's instructions
    timeout: int = DEFAULT_TIMEOUT              # seconds per round
    max_rounds: int = DEFAULT_MAX_ROUNDS
    max_turns: int = DEFAULT_MAX_TURNS
    write: list[str] = field(default_factory=list)    # extra writable hub paths
    commit: list[str] = field(default_factory=list)   # hub-relative paths
    push: bool = False
    enabled: bool = True
    source_path: Path | None = None

    @property
    def scratch_rel(self) -> str:
        """Hub-relative scratch dir the task may always write (state, notes)."""
        return f"{STATE_DIRNAME}/schedule/{self.name}"

    def writable_paths(self) -> list[str]:
        """Hub-relative paths the run may modify.

        `commit:` paths are implicitly writable; `write:` adds writable paths
        that are NOT auto-committed (review-the-diff-by-hand mode); the task's
        scratch dir is always writable.
        """
        seen: dict[str, None] = {}
        for p in [*self.write, *self.commit, self.scratch_rel]:
            seen.setdefault(p)
        return list(seen)


_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _validate_rel_path(raw: Any) -> str:
    """A commit path must stay inside the hub: relative, no `..`, non-empty."""
    p = str(raw).strip().strip("/")
    if not p:
        raise ValueError("empty path")
    if Path(p).is_absolute() or ".." in Path(p).parts:
        raise ValueError(f"path must be hub-relative without '..': {raw!r}")
    return p


def _parse_task(path: Path) -> ScheduledTask:
    fm, body = frontmatter.read(path)
    name = path.stem
    if not _NAME_RE.match(name):
        raise ValueError(f"task filename {name!r} must be alphanumeric/._-")
    raw_schedule = fm.get("schedule")
    if not raw_schedule:
        raise ValueError("missing required `schedule:` (5-field cron) in frontmatter")
    cron = parse_cron(str(raw_schedule))
    if next_fire(cron, datetime.now()) is None:
        raise ValueError(f"cron {raw_schedule!r} never matches a real date")
    if not body.strip():
        raise ValueError("task body is empty — write the instructions below the frontmatter")

    def _int(key: str, default: int) -> int:
        v = fm.get(key, default)
        if not isinstance(v, int) or isinstance(v, bool) or v < 1:
            raise ValueError(f"`{key}:` must be a positive integer, got {v!r}")
        return v

    def _paths(key: str) -> list[str]:
        raw = fm.get(key) or []
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            raise ValueError(f"`{key}:` must be a path or list of hub-relative paths")
        return [_validate_rel_path(p) for p in raw]

    write = _paths("write")
    commit = _paths("commit")

    push = bool(fm.get("push", False))
    if push and not commit:
        raise ValueError("`push: true` needs `commit:` paths — nothing to push otherwise")

    return ScheduledTask(
        name=name,
        schedule=str(raw_schedule).strip(),
        cron=cron,
        body=body,
        timeout=_int("timeout", DEFAULT_TIMEOUT),
        max_rounds=_int("max_rounds", DEFAULT_MAX_ROUNDS),
        max_turns=_int("max_turns", DEFAULT_MAX_TURNS),
        write=write,
        commit=commit,
        push=push,
        enabled=bool(fm.get("enabled", True)),
        source_path=path,
    )


def load_tasks(hub_dir: Path) -> tuple[list[ScheduledTask], list[str]]:
    """Discover and parse every task under `<hub>/schedule/`.

    Returns (tasks, problems). A malformed file becomes a problem string and
    is skipped — one bad task must never take down the scheduler or hide the
    good tasks. Disabled tasks ARE returned (callers filter on `.enabled`)
    so `schedule list` can show them.
    """
    sdir = resolve_bucket(Path(hub_dir), "schedule")
    tasks: list[ScheduledTask] = []
    problems: list[str] = []
    if sdir is None:
        return tasks, problems
    for path in sorted(sdir.glob("*.md"), key=lambda p: p.name.lower()):
        if path.name.startswith(".") or path.name.lower() == "readme.md":
            continue
        try:
            tasks.append(_parse_task(path))
        except (ValueError, OSError) as exc:
            problems.append(f"{path.name}: {exc}")
            log.warning("schedule: skipping %s: %s", path.name, exc)
    return tasks, problems


# ---------------------------------------------------------------------------
# Fire-state: <hub>/.hubzoid/schedule-state.json
# ---------------------------------------------------------------------------
class ScheduleState:
    """Per-task anchors. Shape:

        { "<task>": { "first_seen_at": epoch, "last_fired_at": epoch,
                      "last_result": "done|incomplete|error",
                      "last_run_log": "<path>", "...iso mirrors..." } }

    Epoch seconds are authoritative; `*_iso` keys are human mirrors for anyone
    cat-ing the file on a server.
    """

    def __init__(self, hub_dir: Path):
        self.hub_dir = Path(hub_dir)
        self.path = self.hub_dir / STATE_DIRNAME / STATE_FILENAME

    def _read(self) -> dict[str, dict[str, Any]]:
        if self.path.is_file():
            try:
                data = json.loads(self.path.read_text())
                if isinstance(data, dict):
                    return data
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("schedule-state unreadable (%s); starting fresh", exc)
        return {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.path)              # atomic on POSIX

    def get(self, task_name: str) -> dict[str, Any]:
        return self._read().get(task_name, {})

    def record_seen(self, task_name: str, now: datetime) -> None:
        """Stamp first_seen_at once. A task discovered now anchors *now* — it
        fires at its next future match, not retroactively on install."""
        data = self._read()
        entry = data.setdefault(task_name, {})
        if "first_seen_at" not in entry:
            entry["first_seen_at"] = now.timestamp()
            entry["first_seen_iso"] = now.isoformat(timespec="seconds")
            self._write(data)

    def record_fired(self, task_name: str, when: datetime, *,
                     result: str, run_log: str | None = None) -> None:
        data = self._read()
        entry = data.setdefault(task_name, {})
        entry["last_fired_at"] = when.timestamp()
        entry["last_fired_iso"] = when.isoformat(timespec="seconds")
        entry["last_result"] = result
        if run_log:
            entry["last_run_log"] = run_log
        self._write(data)

    def anchor(self, task_name: str) -> datetime | None:
        entry = self.get(task_name)
        ts = entry.get("last_fired_at") or entry.get("first_seen_at")
        return datetime.fromtimestamp(ts) if ts else None


def next_fire_for(task: ScheduledTask, state: ScheduleState,
                  now: datetime | None = None) -> datetime | None:
    """Next fire computed from the task's anchor (see module docstring).

    Side effect: stamps first_seen_at for never-seen tasks so their anchor
    starts now (no retroactive fire on first discovery).
    """
    now = now or datetime.now()
    anchor = state.anchor(task.name)
    if anchor is None:
        state.record_seen(task.name, now)
        anchor = now
    return next_fire(task.cron, anchor)


def is_due(task: ScheduledTask, state: ScheduleState,
           now: datetime | None = None) -> bool:
    now = now or datetime.now()
    nxt = next_fire_for(task, state, now)
    return nxt is not None and nxt <= now


# ---------------------------------------------------------------------------
# Cross-process run lock: <hub>/.hubzoid/schedule.lock
# ---------------------------------------------------------------------------
class RunLock:
    """One scheduled run at a time per hub, across processes.

    Guards the bridge's scheduler vs a manual `hubzoid schedule run` (or two
    bridges misconfigured onto one hub). A lock whose pid is dead is stale
    and silently stolen — crashes must not wedge the schedule forever.
    """

    def __init__(self, hub_dir: Path):
        self.path = Path(hub_dir) / STATE_DIRNAME / LOCK_FILENAME
        self._held = False

    def _holder_alive(self) -> bool:
        try:
            info = json.loads(self.path.read_text())
            pid = int(info.get("pid", -1))
        except (OSError, ValueError, json.JSONDecodeError):
            return False
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)                  # signal 0: existence probe
        except ProcessLookupError:
            return False
        except PermissionError:
            return True                      # exists, owned by someone else
        return True

    def acquire(self, task_name: str = "") -> bool:
        if self.path.exists() and self._holder_alive():
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({
            "pid": os.getpid(),
            "task": task_name,
            "acquired_at": datetime.now().isoformat(timespec="seconds"),
        }))
        self._held = True
        return True

    def release(self) -> None:
        if self._held:
            self.path.unlink(missing_ok=True)
            self._held = False

    def __enter__(self) -> "RunLock":
        return self

    def __exit__(self, *exc) -> None:
        self.release()
