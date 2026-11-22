# Hubzoid workflows. Apache-2.0 licensed like the rest of the repository.
"""Boot the workflow engine and run the per-minute dispatcher.

Called from the server lifespan (and the CLI). The hub's DBOS engine runs all
scheduled work: markdown `schedule/*.md` tasks and scheduled evals (on whenever
their files exist, as before) and code `workflows/*.py`. Laptop-safety for code
workflows: they only fire when the deployment is marked — `HUBZOID_SCHEDULES=1`
for a single `hubzoid run`, or automatically under `hubzoid gateway`.

This module never imports an agent runtime SDK; the LLM/agent seam
(`context.configure`) is wired by the caller (server.py / cli.py).
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from . import runtime
from .ownership import OwnershipLost

log = logging.getLogger("hubzoid.workflows")

_TRUE = {"1", "true", "yes", "on"}

#: A dispatcher cursor at most this old (seconds) is resumed after a restart:
#: its first tick runs at once and starts the slot that fell inside the gap.
#: An older one is downtime (counted, `catch_up=True` runs the latest once).
RESUME_SECONDS = 150


def _truthy(val: str | None) -> bool:
    return (val or "").strip().lower() in _TRUE


def schedules_enabled(env: dict | None = None) -> bool:
    """Whether this deployment should fire scheduled CODE workflows
    (`workflows/*.py`). Markdown tasks have their own, unchanged gate
    (`markdown_work`)."""
    env = env if env is not None else os.environ
    return _truthy(env.get("HUBZOID_SCHEDULES")) or _truthy(env.get("HUBZOID_GATEWAY"))


def markdown_work(hub_dir, env: dict | None = None) -> bool:
    """Whether the hub has markdown schedule tasks or scheduled evals to run.
    On whenever the files exist, as before; `HUBZOID_DISABLE_SCHEDULE=1` is the
    kill switch."""
    env = env if env is not None else os.environ
    if _truthy(env.get("HUBZOID_DISABLE_SCHEDULE")):
        return False
    from ..evals import schedule as evals_schedule
    from ..scheduling import load_tasks

    tasks, problems = load_tasks(Path(hub_dir))
    return bool(any(t.enabled for t in tasks) or problems
                or evals_schedule.scheduled_cases(Path(hub_dir)))


def _wall() -> float:
    return datetime.now(timezone.utc).timestamp()


def _next_minute(ts: float) -> float:
    """The first minute boundary strictly after `ts` (epoch seconds)."""
    return (ts // 60 + 1) * 60


async def _sleep_until(target: float) -> float:
    """Sleep until the wall clock reaches `target`, never returning before it.

    An event-loop timer can wake a little early: uvloop (which uvicorn uses)
    keeps whole-millisecond timers. A tick a fraction of a millisecond before
    the minute saw no slot, and the next sleep then skipped the whole minute
    (#159). If the wall clock steps back by more than a minute, aim at the
    next boundary instead of sleeping the difference. Returns the target
    reached."""
    while True:
        now = _wall()
        if now >= target:
            return target
        if target - now > 61:
            target = _next_minute(now)
            continue
        await asyncio.sleep(target - now)


def _lift_stale_hold(hub_dir) -> bool:
    """`backup.clear_stale_hold`, never raising: the hold expires on its own."""
    try:
        from ..backup import clear_stale_hold

        return clear_stale_hold(Path(hub_dir))
    except Exception:  # noqa: BLE001
        log.exception("workflows: could not check for a stale backup hold")
        return False


class Dispatcher:
    """Owns the DBOS engine for a hub and the per-minute tick loop."""

    def __init__(self, hub_dir, hub_name: str | None = None, *, code: bool = True):
        self.hub_dir = Path(hub_dir)
        self.hub_name = hub_name
        self.code = code          # dispatch scheduled code workflows too
        self.load_error: str | None = None
        self._task: asyncio.Task | None = None
        self._last = datetime.now(timezone.utc)
        self._resume = False       # tick once at start (a restart's gap)
        self._why: str | None = None   # why the next tick may find skipped slots
        self._held_since: datetime | None = None
        self._n = 0
        self._heartbeat_task = None
        self._events_task = None
        self._alerts_task = None
        self.owner = None
        self.lost = asyncio.Event()   # set when this engine's ownership is lost

    def prepare(self) -> int:
        """Init DBOS over the hub DB, load the code workflows, launch. Returns
        the number of code workflows. A broken workflow module disables code
        workflows for this boot but never the markdown tasks."""
        runtime.init(self.hub_dir, hub_name=self.hub_name)
        self.owner = runtime._OWNER
        self.hub_name = runtime._HUB_NAME
        if self.code:
            try:
                runtime.load_workflows(self.hub_dir)
            except Exception as exc:  # noqa: BLE001
                self.load_error = f"{type(exc).__name__}: {exc}"
                log.exception("workflows: could not load workflows/; code workflows off this boot")
        runtime.launch()
        self._n = len(runtime.registry())
        return self._n

    async def _every(self, seconds: float, fn, what: str):
        """Run `fn` in a thread every `seconds`. A failure is logged and retried;
        a lost ownership ends the loop, and the supervisor stops the engine."""
        while True:
            if getattr(self.owner, "lost", False):
                self.lost.set()
                return
            try:
                await asyncio.to_thread(fn)
            except asyncio.CancelledError:
                raise
            except OwnershipLost:
                self.lost.set()
                return
            except Exception:
                log.exception("workflows: %s failed; will retry", what)
            await asyncio.sleep(seconds)

    def _beat(self):
        self.owner.heartbeat(runtime.ready_record())
        from ..access import store_for
        store_for(self.hub_dir).set_runtime_health(
            self.hub_dir.name, heartbeat=datetime.now(timezone.utc).isoformat(), enabled=True)

    def _heartbeat(self):
        return self._every(15, self._beat, "ownership heartbeat (admission closed)")

    def _alerts(self):
        from . import alerts
        return self._every(30, lambda: alerts.reconcile(self.hub_dir, self.hub_name),
                           "alert reconciliation")

    def _events(self):
        from . import events
        return self._every(1, lambda: events.reconcile(self.hub_dir, self.hub_dir.name),
                           "event reconciliation")

    async def _loop(self) -> None:
        """One tick per minute boundary. Each tick looks at (last, now], so
        a boundary is ticked once even when a timer wakes early or a tick
        is slow. Only a tick that itself runs past the next boundary skips
        one, and the next tick counts the folded slot with a warning."""
        aim = _next_minute(_wall())
        immediate, self._resume = self._resume, False
        while True:
            if immediate:
                immediate, reached = False, _wall()
            else:
                reached = await _sleep_until(aim)
            now = datetime.fromtimestamp(max(_wall(), reached), timezone.utc)
            error = None
            try:
                started = await asyncio.to_thread(
                    runtime.tick, last=self._last, now=now, why=self._why
                )
                if started is None:
                    # Held for a backup: slots inside the hold fire when it
                    # ends, and the tick that finds them says why they were
                    # folded. A hold left by a backup that died is lifted.
                    self._held_since = self._held_since or now
                    self._why = (f"new runs were held for a backup from "
                                 f"{self._held_since.isoformat(timespec='seconds')}")
                    await asyncio.to_thread(_lift_stale_hold, self.hub_dir)
                else:
                    if started:
                        log.info("workflows: fired %s", started)
                    self._last, self._why, self._held_since = now, None, None
            except runtime.DispatchFailed as exc:
                # Some workflows could not be queued. Their slots are already
                # counted and warned; the others started. Move on.
                error = f"{type(exc).__name__}: dispatch failed; check server logs"
                log.exception("workflows: dispatcher tick failed")
                self._last, self._why, self._held_since = now, None, None
            except Exception as exc:  # noqa: BLE001 — the loop must never die
                # The tick failed before it looked at the workflows (the store
                # was unreachable, say). Keep the cursor: the next tick covers
                # this window too, runs its latest slot and counts the rest.
                error = f"{type(exc).__name__}: dispatch failed; check server logs"
                log.exception("workflows: dispatcher tick failed")
                self._why = (f"the dispatch at {now.isoformat(timespec='seconds')} failed "
                             f"({type(exc).__name__})")
            from ..access import store_for

            try:
                store_for(self.hub_dir).set_runtime_health(
                    self.hub_dir.name,
                    heartbeat=now.isoformat(),
                    last_tick=self._last.isoformat(),
                    enabled=True,
                    error=error or self.load_error,
                )
            except Exception:  # a transient health write must not kill dispatch
                log.exception("workflows: could not record dispatcher health")
            # The boundary after the one just ticked. If the tick itself ran
            # past it, the next sleep returns at once and that slot still runs.
            aim = _next_minute(now.timestamp())

    def start_loop(self, resume_from: datetime | None = None) -> None:
        """Start the per-minute loop. `resume_from` is the previous
        dispatcher's cursor after a short restart: the first tick runs at
        once over (resume_from, now], so the slot inside the gap starts (its
        run id is fixed, so a slot the old process already started is not
        started again)."""
        if self._task is None:
            if resume_from is not None:
                self._last, self._resume = resume_from, True
                self._why = (f"the hub restarted (previous dispatch at "
                             f"{resume_from.isoformat(timespec='seconds')})")
            else:
                self._last = datetime.now(timezone.utc)
            self._task = asyncio.create_task(self._loop())
            log.info("workflows: dispatcher started (%d workflow(s))", self._n)

    async def stop(self) -> None:
        if self._alerts_task is not None:
            self._alerts_task.cancel()
            try:
                await self._alerts_task
            except asyncio.CancelledError:
                pass
            self._alerts_task = None
        if self._events_task is not None:
            self._events_task.cancel()
            try:
                await self._events_task
            except asyncio.CancelledError:
                pass
            self._events_task = None
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        # Tear DBOS down cleanly so the bridge does not leak the worker. Runs
        # still in flight stay recoverable and resume on the next launch.
        try:
            from ..access import store_for

            store_for(self.hub_dir).set_runtime_health(self.hub_dir.name, enabled=False)
        except Exception:  # noqa: BLE001 — health write is best-effort
            log.exception("workflows: could not mark dispatcher stopped")
        await asyncio.to_thread(runtime.shutdown)


OWNER_RETRY_SECONDS = 15


class EngineSupervisor:
    """The bridge's handle on its workflow engine. While another process owns
    the hub (a CLI run, or a previous owner's lease), and after this one loses
    ownership, the engine is retried every OWNER_RETRY_SECONDS; chat never
    waits for it."""

    def __init__(self, hub_dir, hub_name, dispatcher=None):
        self.hub_dir, self.hub_name = Path(hub_dir), hub_name
        self.dispatcher = dispatcher
        self._attempt = None
        self.task = asyncio.create_task(self._run())

    async def _run(self):
        from ..access import store_for
        while True:
            if self.dispatcher is not None:
                await self.dispatcher.lost.wait()
                lost, self.dispatcher = self.dispatcher, None
                await lost.stop()
                await asyncio.to_thread(store_for(self.hub_dir).set_runtime_health,
                    self.hub_dir.name, enabled=True,
                    error="Workflow engine ownership was lost; retrying. Chat is unaffected.")
            await asyncio.sleep(OWNER_RETRY_SECONDS)
            # A start in progress is never abandoned half way: stop() waits for it.
            self._attempt = asyncio.ensure_future(_start_engine(self.hub_dir, self.hub_name))
            disp, busy = await asyncio.shield(self._attempt)
            self._attempt = None
            self.dispatcher = disp
            if disp is None and not busy:
                return   # off for a reason a retry cannot fix; health says why

    async def stop(self):
        self.task.cancel()
        try:
            await self.task
        except asyncio.CancelledError:
            pass
        if self._attempt is not None:
            disp, _ = await self._attempt
            self.dispatcher = self.dispatcher or disp
        if self.dispatcher is not None:
            await self.dispatcher.stop()


async def start(hub_dir, hub_name: str | None = None):
    """Start the hub's DBOS engine when there is scheduled work: markdown tasks
    or scheduled evals (on whenever their files exist), or code workflows (when
    schedules are enabled for this deployment, or a webhook declares one).
    Starts the per-minute tick for code workflows. Returns an EngineSupervisor
    (to stop() at shutdown), or None when the engine stays off."""
    disp, busy = await _start_engine(hub_dir, hub_name)
    if disp is None and not busy:
        return None
    return EngineSupervisor(hub_dir, hub_name, disp)


def _scheduled_code(hub_dir) -> list[str]:
    """Code workflows in this hub that declare a schedule."""
    try:
        from .observe import definitions

        return [d["name"] for d in definitions(hub_dir) if d.get("schedule") and not d["name"].startswith("md:")]
    except Exception:  # noqa: BLE001 - a warning helper never stops the start
        return []


async def _start_engine(hub_dir, hub_name):
    """(dispatcher, owner_busy). Never raises: chat must start regardless."""
    from ..access import store_for
    from .ownership import OwnerBusy

    gs = store_for(hub_dir)
    from .events import declarations
    try:
        webhook_on = bool(declarations(hub_dir))
    except Exception as exc:
        gs.set_runtime_health(Path(hub_dir).name, enabled=False, error=f"{type(exc).__name__}: invalid workflow settings; check logs")
        log.exception("workflows: invalid configuration; chat remains available")
        return None, False
    code_on = schedules_enabled()
    md_on = await asyncio.to_thread(markdown_work, hub_dir)
    if not code_on and not md_on and not webhook_on:
        gs.set_runtime_health(Path(hub_dir).name, enabled=False, error=None, schedules=False)
        log.info(
            "workflows: schedules idle (set HUBZOID_SCHEDULES=1 to enable on this box)"
        )
        return None, False
    disp = Dispatcher(hub_dir, hub_name, code=code_on or webhook_on)
    try:
        n = await asyncio.to_thread(disp.prepare)
    except Exception as exc:  # the engine failed to start; chat still works
        if isinstance(exc, OwnerBusy):
            gs.set_runtime_health(Path(hub_dir).name, enabled=True,
                                  error='Waiting for the current workflow owner to release the hub')
            log.warning('workflows: %s; retrying without interrupting chat', exc)
            return None, True
        await asyncio.to_thread(runtime.shutdown)
        gs.set_runtime_health(
            Path(hub_dir).name, enabled=False, error=f"{type(exc).__name__}: {exc}"
        )
        log.exception("workflows: engine failed to start; scheduled work off this boot")
        return None, False
    if n == 0 and not md_on:
        await disp.stop()
        if disp.load_error:
            # A webhook-only hub whose module failed: keep the error, so the
            # edge health check and its alert report it.
            gs.set_runtime_health(Path(hub_dir).name, enabled=False, error=disp.load_error)
        log.info("workflows: none defined under <hub>/workflows/")
        return None, False
    if not code_on and n and _scheduled_code(hub_dir):
        # The engine runs (for webhooks or schedule/*.md), but code workflows'
        # timetables don't fire on this machine: say so, here and in Settings.
        log.warning("workflows: schedules are off on this machine, so the scheduled code workflows of %s "
                    "will not run here. Set HUBZOID_SCHEDULES=1 to run them.", Path(hub_dir).name)
    await asyncio.to_thread(_lift_stale_hold, hub_dir)
    disp.owner = disp.owner or runtime._OWNER
    if hasattr(disp.owner, "on_lost"):
        loop = asyncio.get_running_loop()
        disp.owner.on_lost(lambda _reason: loop.call_soon_threadsafe(disp.lost.set))
    now = datetime.now(timezone.utc)
    # Where the previous dispatcher got to: its last tick (`last_tick`), or the
    # heartbeat for records written before 2.1.2. A short gap is resumed: the
    # first tick runs at once and starts the slot inside it. A longer one is
    # downtime: recorded (but never back-filled) so the operator sees the gap
    # (`downtime`, dated per workflow in `missed_log` for the Console), and
    # `catch_up=True` workflows run their latest slot once. (Markdown tasks
    # catch up once by their own anchor rule.)
    downtime = None
    resume_from = None
    cursor_written = None
    prior = gs.runtime_health(Path(hub_dir).name)
    prior_cursor = prior.get("last_tick") or prior.get("heartbeat")
    if prior_cursor and n and code_on:   # nothing to catch up when schedules are off
        try:
            since = datetime.fromisoformat(prior_cursor)
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
            if (now - since).total_seconds() <= RESUME_SECONDS:
                resume_from = since
            else:
                cursor_written = now.isoformat()
                # The loop starts from this scan, so a slot between the scan
                # and the loop's first tick is not lost either.
                resume_from = now
                window = await asyncio.to_thread(runtime.downtime_missed, since, now)
                caught = []
                if window.get("missed"):
                    caught = await asyncio.to_thread(runtime.catch_up, since, now)
                    if caught:
                        log.warning("workflows: caught up once late: %s", ", ".join(caught))
                    window = runtime.without_caught_up(window, caught)
                if window.get("missed"):
                    downtime = window
                    log.warning(
                        "workflows: %d scheduled slot(s) missed during downtime "
                        "%s..%s (not back-filled): %s",
                        window["missed"],
                        window["since"],
                        window["until"],
                        ", ".join(f"{k} {v}" for k, v in window["by_workflow"].items()),
                    )
        except ValueError:
            pass
    if cursor_written:
        extra = {"last_tick": cursor_written}
    elif not (n and code_on):
        # No dispatcher loop this boot: a cursor from an earlier boot would read
        # as downtime once schedules are on again. Fall back to the heartbeat.
        extra = {"last_tick": None}
    else:
        extra = {}
    gs.set_runtime_health(
        Path(hub_dir).name,
        enabled=True,
        schedules=code_on,
        error=disp.load_error,
        downtime=downtime,
        missed_log=runtime.missed_log(prior, 0, now,
                                      by=downtime["by_workflow"] if downtime else None),
        heartbeat=now.isoformat(),
        **extra,
    )
    try:
        await asyncio.to_thread(disp.owner.heartbeat, runtime.ready_record())
    except OwnershipLost:
        await disp.stop()
        return None, True
    disp._heartbeat_task = asyncio.create_task(disp._heartbeat())
    disp._events_task = asyncio.create_task(disp._events())
    disp._alerts_task = asyncio.create_task(disp._alerts())
    if code_on and n:
        disp.start_loop(resume_from)
    return disp, False
