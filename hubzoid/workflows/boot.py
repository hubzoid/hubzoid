# Hubzoid workflows. MIT licensed like the rest of the repository.
"""Boot the workflow engine and run the per-minute dispatcher.

Called from the server lifespan (and the CLI). Laptop-safety: workflows only
fire when the deployment is marked — `HUBZOID_SCHEDULES=1` for a single
`hubzoid run`, or automatically under `hubzoid gateway`. A bare laptop `run`
loads nothing here, so a laptop never fires production.

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
    """Whether this deployment should fire scheduled workflows. DBOS-workflows
    only — the markdown agent-task scheduler has its own, unchanged gate."""
    env = env if env is not None else os.environ
    return _truthy(env.get("HUBZOID_SCHEDULES")) or _truthy(env.get("HUBZOID_GATEWAY"))


class Dispatcher:
    """Owns the DBOS engine for a hub and the per-minute tick loop."""

    def __init__(self, hub_dir, hub_name: str | None = None):
        self.hub_dir = Path(hub_dir)
        self.hub_name = hub_name
        self._task: asyncio.Task | None = None
        self._last = datetime.now(timezone.utc)
        self._n = 0

    def prepare(self) -> int:
        """Init DBOS over the hub DB, load the workflows, launch. Returns count."""
        runtime.init(self.hub_dir, hub_name=self.hub_name)
        n = runtime.load_workflows(self.hub_dir)
        runtime.launch()
        self._n = len(runtime.registry())
        return self._n

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            now = datetime.now(timezone.utc)
            try:
                started = await asyncio.to_thread(runtime.tick, last=self._last, now=now)
                if started:
                    log.info("workflows: fired %s", started)
            except Exception:  # noqa: BLE001 — the loop must never die
                log.exception("workflows: dispatcher tick failed")
            self._last = now

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


async def start(hub_dir, hub_name: str | None = None) -> Dispatcher | None:
    """Prepare + start the dispatcher if schedules are enabled and workflows
    exist. Returns the Dispatcher (to stop() at shutdown) or None."""
    if not schedules_enabled():
        log.info(
            "workflows: schedules idle (set HUBZOID_SCHEDULES=1 to enable on this box)"
        )
        return None
    disp = Dispatcher(hub_dir, hub_name)
    try:
        n = await asyncio.to_thread(disp.prepare)
    except Exception:  # noqa: BLE001 — a bad workflow module never blocks chat
        log.exception("workflows: failed to prepare; schedules disabled this boot")
        return None
    if n == 0:
        log.info("workflows: none defined under <hub>/workflows/")
        return None
    disp.start_loop()
    return disp
