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

    def prepare(self) -> int:
        """Init DBOS over the hub DB, load the code workflows, launch. Returns
        the number of code workflows. A broken workflow module disables code
        workflows for this boot but never the markdown tasks."""
        runtime.init(self.hub_dir, hub_name=self.hub_name)
        if self.code:
            try:
                runtime.load_workflows(self.hub_dir)
            except Exception as exc:  # noqa: BLE001
                self.load_error = f"{type(exc).__name__}: {exc}"
                log.exception("workflows: could not load workflows/; code workflows off this boot")
        runtime.launch()
        self._n = len(runtime.registry())
        return self._n

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
                    error=error,
                )
            except Exception:  # a transient health write must not kill dispatch
                log.exception("workflows: could not record dispatcher health")

    def start_loop(self) -> None:
        if self._task is None:
            self._last = datetime.now(timezone.utc)
            self._task = asyncio.create_task(self._loop())
            log.info("workflows: dispatcher started (%d workflow(s))", self._n)

    async def stop(self) -> None:
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


async def start(hub_dir, hub_name: str | None = None) -> Dispatcher | None:
    """Start the hub's DBOS engine when there is scheduled work: markdown tasks
    or scheduled evals (on whenever their files exist), or code workflows (when
    schedules are enabled for this deployment). Starts the per-minute tick for
    code workflows. Returns the Dispatcher (to stop() at shutdown) or None."""
    from ..access import store_for

    gs = store_for(hub_dir)
    code_on = schedules_enabled()
    md_on = await asyncio.to_thread(markdown_work, hub_dir)
    if not code_on and not md_on:
        gs.set_runtime_health(Path(hub_dir).name, enabled=False, error=None)
        log.info(
            "workflows: schedules idle (set HUBZOID_SCHEDULES=1 to enable on this box)"
        )
        return None
    disp = Dispatcher(hub_dir, hub_name, code=code_on)
    try:
        n = await asyncio.to_thread(disp.prepare)
    except Exception as exc:  # the engine failed to start; chat still works
        await asyncio.to_thread(runtime.shutdown)
        gs.set_runtime_health(
            Path(hub_dir).name, enabled=False, error=f"{type(exc).__name__}: {exc}"
        )
        log.exception("workflows: engine failed to start; scheduled work off this boot")
        return None
    if n == 0 and not md_on:
        await disp.stop()
        log.info("workflows: none defined under <hub>/workflows/")
        return None
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
    if code_on and n:
        disp.start_loop()
    return disp
