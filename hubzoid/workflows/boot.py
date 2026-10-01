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


class Dispatcher:
    """Owns the DBOS engine for a hub and the per-minute tick loop."""

    def __init__(self, hub_dir, hub_name: str | None = None, *, code: bool = True):
        self.hub_dir = Path(hub_dir)
        self.hub_name = hub_name
        self.code = code          # dispatch scheduled code workflows too
        self.load_error: str | None = None
        self._task: asyncio.Task | None = None
        self._last = datetime.now(timezone.utc)
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
        while True:
            await asyncio.sleep(
                max(0.1, 60 - datetime.now(timezone.utc).timestamp() % 60)
            )
            now = datetime.now(timezone.utc)
            error = None
            held = False
            try:
                started = await asyncio.to_thread(
                    runtime.tick, last=self._last, now=now
                )
                held = started is None
                if started:
                    log.info("workflows: fired %s", started)
            except Exception as exc:  # noqa: BLE001 — the loop must never die
                error = f"{type(exc).__name__}: dispatch failed; check server logs"
                log.exception("workflows: dispatcher tick failed")
            if not held:
                self._last = now
            from ..access import store_for

            try:
                store_for(self.hub_dir).set_runtime_health(
                    self.hub_dir.name,
                    heartbeat=now.isoformat(),
                    enabled=True,
                    error=error or self.load_error,
                )
            except Exception:  # a transient health write must not kill dispatch
                log.exception("workflows: could not record dispatcher health")

    def start_loop(self) -> None:
        if self._task is None:
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
        gs.set_runtime_health(Path(hub_dir).name, enabled=False, error=None)
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
    disp.owner = disp.owner or runtime._OWNER
    if hasattr(disp.owner, "on_lost"):
        loop = asyncio.get_running_loop()
        disp.owner.on_lost(lambda _reason: loop.call_soon_threadsafe(disp.lost.set))
    now = datetime.now(timezone.utc)
    # Record (but never back-fill) any scheduled code-workflow slots that would
    # have fired while the previous dispatcher was down, so the operator sees the
    # gap (`downtime`, and dated in `missed_log` for the Console). (Markdown
    # tasks catch up once by their own anchor rule.)
    downtime = None
    prior = gs.runtime_health(Path(hub_dir).name)
    prior_beat = prior.get("heartbeat")
    if prior_beat and n:
        try:
            since = datetime.fromisoformat(prior_beat)
            if since.tzinfo is None:
                since = since.replace(tzinfo=timezone.utc)
            if (now - since).total_seconds() > 90:
                window = await asyncio.to_thread(runtime.downtime_missed, since, now)
                if window.get("missed"):
                    downtime = window
                    log.warning(
                        "workflows: %d scheduled slot(s) missed during downtime "
                        "%s..%s (not back-filled)",
                        window["missed"],
                        window["since"],
                        window["until"],
                    )
        except ValueError:
            pass
    gs.set_runtime_health(
        Path(hub_dir).name,
        enabled=True,
        error=disp.load_error,
        downtime=downtime,
        missed_log=runtime.missed_log(prior, downtime["missed"] if downtime else 0, now),
        heartbeat=now.isoformat(),
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
        disp.start_loop()
    return disp, False
