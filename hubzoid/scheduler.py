"""In-process scheduler — fires `<hub>/schedule/*.md` tasks while the hub runs.

Started by the FastAPI bridge's lifespan (`server.build_app`), so deploying a
hub IS deploying its background jobs: one long-lived process, no extra
systemd units or crontabs. Mirrors the lifecycle of Claude Code's
cronScheduler:

  * a cheap **tick** every `tick_seconds` (default 30): re-load the task
    files (picking up live edits — the md files are the source of truth),
    compute due-ness from each task's anchor, and fire what's due;
  * **idle gate**: a task never starts while a chat request is in flight
    (`is_busy()`); it stays due and fires on a later tick;
  * **missed-run catch-up** is inherent in the anchor model (see
    `scheduling.py`): downtime across a cron match makes the task due on the
    first tick after startup — it fires once, not once per missed match;
  * **one run at a time**, cross-process: the `RunLock` also excludes a
    concurrent manual `hubzoid schedule run`;
  * kill switch: `HUBZOID_DISABLE_SCHEDULE=1` disables the loop entirely.

Failure containment: a crashing task run is logged and recorded in
schedule-state; the loop itself never dies. Because `record_fired` stamps at
run *start*, a crashing task waits for its next cron match instead of
re-firing every tick.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import schedule_runner
from .scheduling import RunLock, ScheduledTask, ScheduleState, is_due, load_tasks

log = logging.getLogger("hubzoid.schedule")

DISABLE_ENV = "HUBZOID_DISABLE_SCHEDULE"
DEFAULT_TICK_SECONDS = 30


def _truthy(val: str | None) -> bool:
    return (val or "").strip().lower() in ("1", "true", "yes", "on")


def _ensure_visible(logger: logging.Logger) -> None:
    """Make sure scheduler lines reach stderr even under uvicorn's logging
    config (which only wires its own loggers). No-op if anything up the
    hierarchy already has a handler."""
    l: logging.Logger | None = logger
    while l:
        if l.handlers:
            return
        l = l.parent if l.propagate else None
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(name)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    if logger.level == logging.NOTSET:
        logger.setLevel(logging.INFO)


class Scheduler:
    """One per hub process. `start()` spawns the tick task; `stop()` joins it."""

    def __init__(
        self,
        hub_dir: Path,
        *,
        is_busy: Callable[[], bool] = lambda: False,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
        run_task: Callable = schedule_runner.run_task,   # injectable for tests
    ):
        self.hub_dir = Path(hub_dir).resolve()
        self.is_busy = is_busy
        self.tick_seconds = tick_seconds
        self._run_task = run_task
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._deferred_logged: set[str] = set()

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> bool:
        """Spawn the tick loop. Returns False when disabled or nothing to do."""
        if _truthy(os.environ.get(DISABLE_ENV)):
            log.info("scheduler disabled via %s", DISABLE_ENV)
            return False
        _ensure_visible(log)
        tasks, problems = load_tasks(self.hub_dir)
        for p in problems:
            log.warning("schedule: %s", p)
        enabled = [t for t in tasks if t.enabled]
        if not enabled and not problems:
            log.info("scheduler: no tasks under %s/schedule — not starting",
                     self.hub_dir.name)
            return False
        state = ScheduleState(self.hub_dir)
        now = datetime.now()
        for t in enabled:
            from .scheduling import next_fire_for
            nxt = next_fire_for(t, state, now)
            log.info("scheduler: %s (%s) next fire %s", t.name, t.schedule,
                     nxt.strftime("%Y-%m-%d %H:%M") if nxt else "never")
        self._stopping.clear()
        self._task = asyncio.create_task(self._loop())   # caller has a running loop (lifespan)
        log.info("scheduler started for hub %s (%d task(s), tick %.0fs)",
                 self.hub_dir.name, len(enabled), self.tick_seconds)
        return True

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    # -- the loop ----------------------------------------------------------
    async def _loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.check_once()
            except Exception:  # noqa: BLE001 — the loop must survive anything
                log.exception("scheduler tick failed (continuing)")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=self.tick_seconds)
            except asyncio.TimeoutError:
                pass

    async def check_once(self, now: datetime | None = None) -> list[str]:
        """One tick: fire every due task sequentially. Returns names fired.

        Tasks are re-loaded from disk each tick so edits to schedule/*.md
        apply live, without a hub restart — same spirit as Claude Code
        watching scheduled_tasks.json.
        """
        now = now or datetime.now()
        tasks, _problems = load_tasks(self.hub_dir)
        state = ScheduleState(self.hub_dir)
        fired: list[str] = []
        for task in tasks:
            if not task.enabled:
                continue
            if not is_due(task, state, now):
                self._deferred_logged.discard(task.name)
                continue
            if self.is_busy():
                if task.name not in self._deferred_logged:
                    log.info("schedule[%s] due but hub is busy; deferring to a "
                             "later tick", task.name)
                    self._deferred_logged.add(task.name)
                continue
            self._deferred_logged.discard(task.name)
            if await self._fire(task):
                fired.append(task.name)
        return fired

    async def _fire(self, task: ScheduledTask) -> bool:
        """Run the task under the cross-process lock. False = lock-skipped."""
        lock = RunLock(self.hub_dir)
        if not lock.acquire(task.name):
            log.info("schedule[%s] due but another run holds the lock; "
                     "skipping this tick", task.name)
            return False
        try:
            log.info("schedule[%s] firing (%s)", task.name, task.schedule)
            result = await self._run_task(self.hub_dir, task)
            log.info("schedule[%s] finished: %s (%d round(s)%s)",
                     task.name, result.result, result.rounds,
                     f", commit {result.commit_sha[:10]}" if result.commit_sha else "")
        except Exception:  # noqa: BLE001 — run_task shouldn't raise, but belt+braces
            log.exception("schedule[%s] run crashed", task.name)
        finally:
            lock.release()
        return True
