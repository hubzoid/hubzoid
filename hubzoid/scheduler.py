"""The markdown-task scheduler: decides WHEN `<hub>/schedule/*.md` tasks and
`<hub>/evals/*.md` cases are due, and hands each due one to DBOS to run.

Execution is on the hub's DBOS engine (`workflows/markdown.py`): each due task
becomes one durable run on the hub's markdown queue (one run at a time per hub,
across processes), visible in the run history. This module keeps the timing
rules builders already rely on:

  * a cheap **tick** every `tick_seconds` (default 30): re-load the task files
    (edits apply live), compute due-ness from each task's anchor, dispatch;
  * **idle gate**: a task is not dispatched while a chat request is in flight
    (`is_busy()`); it stays due and goes on a later tick;
  * **missed-run catch-up** is inherent in the anchor model (see
    `scheduling.py`): downtime across a cron match makes the task due on the
    first tick after startup, once, not once per missed match;
  * **machine-local time** for cron expressions;
  * kill switch: `HUBZOID_DISABLE_SCHEDULE=1` disables the loop entirely.

A dispatch stamps the task as fired, so it waits for its next match. The run's
id is `md:<task>:<slot>`, so a slot is never queued twice even if two ticks (or
two processes) see it due.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Callable

from .evals import schedule as evals_schedule
from .scheduling import ScheduledTask, ScheduleState, is_due, load_tasks

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
        dispatch_task: Callable | None = None,    # (task, slot, claimed) -> None
        dispatch_evals: Callable | None = None,   # (names, now) -> None
    ):
        self.hub_dir = Path(hub_dir).resolve()
        self.is_busy = is_busy
        self.tick_seconds = tick_seconds
        self._dispatch_task = dispatch_task or _dbos_dispatch_task
        self._dispatch_evals = dispatch_evals or _dbos_dispatch_evals
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._deferred_logged: set[str] = set()
        self._dispatched: set[str] = set()   # webhook runs already queued

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
        evals = evals_schedule.scheduled_cases(self.hub_dir)
        if not enabled and not problems and not evals:
            log.info("scheduler: no tasks under %s/schedule and no scheduled "
                     "evals — not starting", self.hub_dir.name)
            return False
        state = ScheduleState(self.hub_dir)
        now = datetime.now()
        for t in enabled:
            if t.is_webhook:
                log.info("scheduler: %s (on webhook %s) fires on event", t.name, t.on_webhook)
                continue
            from .scheduling import next_fire_for
            nxt = next_fire_for(t, state, now)
            log.info("scheduler: %s (%s) next fire %s", t.name, t.schedule,
                     nxt.strftime("%Y-%m-%d %H:%M") if nxt else "never")
        for c in evals:
            nxt = evals_schedule.next_fire_for(c, state, now)
            log.info("scheduler: eval %s (%s) next fire %s", c.name, c.schedule,
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
        if _held(self.hub_dir):
            return []  # a backup is running; due tasks fire when it ends
        tasks, _problems = load_tasks(self.hub_dir)
        state = ScheduleState(self.hub_dir)
        paused = _paused(self.hub_dir)
        fired: list[str] = []
        for task in tasks:
            if not task.enabled or f"md:{task.name}" in paused:
                continue
            if not is_due(task, state, now, hub_dir=self.hub_dir):
                self._deferred_logged.discard(task.name)
                continue
            if self.is_busy():
                if task.name not in self._deferred_logged:
                    log.info("schedule[%s] due but hub is busy; deferring to a "
                             "later tick", task.name)
                    self._deferred_logged.add(task.name)
                continue
            self._deferred_logged.discard(task.name)
            if await self._fire(task, now):
                fired.append(task.name)

        fired += await self._check_evals(state, now)
        return fired

    async def _check_evals(self, state: ScheduleState, now: datetime) -> list[str]:
        """Fire every due eval case as ONE suite run.

        Batched because building the runtime (MCP init) is the expensive part:
        five cases sharing a weekly cron should cost one startup, not five.
        Due-ness is still per case, so a case added later catches up on its own
        schedule rather than inheriting the batch's anchor.
        """
        due = evals_schedule.due_cases(self.hub_dir, state, now)
        if not due:
            return []

        names = [c.name for c in due]
        if self.is_busy():
            key = "evals:" + ",".join(names)
            if key not in self._deferred_logged:
                log.info("evals due (%s) but hub is busy; deferring to a later "
                         "tick", ", ".join(names))
                self._deferred_logged.add(key)
            return []
        self._deferred_logged = {k for k in self._deferred_logged
                                 if not k.startswith("evals:")}

        try:
            await asyncio.to_thread(self._dispatch_evals, names, now)
        except Exception:  # noqa: BLE001 — the tick loop must survive anything
            log.exception("could not queue the due evals")
            return []
        for c in due:  # stamped now, so they wait for their next match
            state.record_fired(evals_schedule.state_key(c), now, result="queued")
        log.info("evals queued: %s", ", ".join(names))
        return [f"eval:{n}" for n in names]

    async def _fire(self, task: ScheduledTask, now: datetime) -> bool:
        """Queue one run of `task` on DBOS and stamp it as fired."""
        from .scheduling import next_fire_for

        claimed: list[str] = []
        if task.is_webhook:
            # The events this run is responsible for. They are archived only
            # when the run finishes DONE, so a failed run leaves them pending.
            from .inbound.webhook import pending_events

            claimed = [str(p) for p in pending_events(self.hub_dir, task.on_webhook)]
            import hashlib

            slot = "events-" + hashlib.sha256("|".join(sorted(claimed)).encode()).hexdigest()[:16]
            if f"{task.name}:{slot}" in self._dispatched:
                return False  # already queued; its events stay pending until it finishes
        else:
            due_at = next_fire_for(task, ScheduleState(self.hub_dir), now) or now
            slot = due_at.strftime("%Y%m%dT%H%M")
        trigger = f"on_webhook {task.on_webhook}" if task.is_webhook else task.schedule
        try:
            await asyncio.to_thread(self._dispatch_task, task, slot, claimed)
        except Exception:  # noqa: BLE001 — the tick loop must survive anything
            log.exception("schedule[%s] could not be queued", task.name)
            return False
        if task.is_webhook:
            self._dispatched.add(f"{task.name}:{slot}")
        else:
            ScheduleState(self.hub_dir).record_fired(task.name, now, result="queued")
        log.info("schedule[%s] queued (%s, slot %s)", task.name, trigger, slot)
        return True


def _paused(hub_dir: Path) -> set[str]:
    """Names the operator paused (`hubzoid schedule pause`); empty if unreadable."""
    try:
        from .access import store_for

        return store_for(hub_dir).paused_workflows(Path(hub_dir).name)
    except Exception:  # noqa: BLE001 — a store hiccup must not stop the schedule
        log.warning("scheduler: could not read paused tasks", exc_info=True)
        return set()


def _held(hub_dir: Path) -> bool:
    """True while `hubzoid backup` holds new scheduled runs. A store hiccup
    reads as no hold: a backup only delays runs, it must never lose them."""
    try:
        from .access import store_for

        hold = store_for(hub_dir).schedule_hold()
    except Exception:  # noqa: BLE001
        log.warning("scheduler: could not read the backup hold", exc_info=True)
        return False
    if hold:
        log.debug("scheduler: new runs held (%s)", hold.get("reason"))
    return bool(hold)


def _dbos_dispatch_task(task: ScheduledTask, slot: str, claimed: list[str]) -> None:
    from .workflows import markdown

    markdown.enqueue_task(task.name, slot, claimed)


def _dbos_dispatch_evals(names: list[str], now: datetime) -> None:
    from .workflows import markdown

    markdown.enqueue_evals(names, now)
