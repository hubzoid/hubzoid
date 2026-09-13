# Hubzoid workflows. MIT licensed like the rest of the repository.
"""The DBOS execution layer: `@workflow`, `@step`, and the in-zone dispatcher.

DBOS is the durable-execution engine, embedded (no server). It is invisible to
the author, who only writes `@workflow` / `@step` / `hub`. DBOS shares the ONE
hub database (its system tables live alongside `hz_*`), SQLite by default.

Boot order (server lifespan or CLI):
    init(hub_dir)              # construct the DBOS singleton over the hub DB
    load_workflows(hub_dir)    # import workflows/<name>/*.py (applies @workflow)
    launch()                   # DBOS.launch()
    # then a per-minute dispatcher calls tick() to fire due workflows

Durability is at-least-once, not exactly-once: a completed step is resumed from
its checkpoint on restart; an interrupted step can re-run, so side effects must
be idempotent.
"""
from __future__ import annotations

import importlib.util
import logging
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .. import db
from . import context

log = logging.getLogger("hubzoid.workflows")

_DBOS = None                     # the DBOS class, imported lazily
_INITED = False
_LAUNCHED = False
_HUB_DIR: Path | None = None
_HUB_NAME: str = ""
_ENGINE: Any = None
_lock = threading.Lock()


@dataclass
class WorkflowDef:
    name: str
    fn: Callable
    wrapped: Callable            # the DBOS-wrapped callable
    schedule: str | None
    timezone: str | None
    on_failure: str | None
    max_attempts: int


_REGISTRY: "dict[str, WorkflowDef]" = {}


def _app_name(hub_name: str) -> str:
    """DBOS app name: 3–30 chars, lowercase, alnum + hyphen."""
    slug = re.sub(r"[^a-z0-9-]", "-", hub_name.lower()).strip("-") or "hub"
    slug = f"hz-{slug}"[:30]
    if len(slug) < 3:
        slug = (slug + "-hub")[:30]
    return slug


def init(hub_dir, hub_name: str | None = None) -> None:
    """Construct the DBOS singleton over this hub's database. Idempotent. Must
    run before any workflow module is imported (the decorator needs DBOS)."""
    global _DBOS, _INITED, _HUB_DIR, _HUB_NAME, _ENGINE
    with _lock:
        if _INITED:
            return
        from dbos import DBOS

        _DBOS = DBOS
        _HUB_DIR = Path(hub_dir)
        _HUB_NAME = hub_name or _HUB_DIR.name
        _ENGINE = db.engine_for(_HUB_DIR)
        sys_url = db.resolve_url(_HUB_DIR)
        DBOS(config={"name": _app_name(_HUB_NAME), "system_database_url": sys_url})
        _INITED = True
        log.info("workflows: DBOS initialised for hub %r", _HUB_NAME)


def _require_init():
    if not _INITED:
        raise RuntimeError(
            "workflows.init(hub_dir) must be called before defining workflows"
        )


def _load_settings() -> dict:
    """Admin-settable config for the current hub's workflows. Kept minimal:
    reads workflows/settings.yaml if present, else {}."""
    if _HUB_DIR is None:
        return {}
    path = _HUB_DIR / "workflows" / "settings.yaml"
    if not path.exists():
        return {}
    try:
        import yaml

        data = yaml.safe_load(path.read_text()) or {}
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001 — bad config never crashes a run
        log.warning("workflows: could not read %s", path)
        return {}


def workflow(schedule: str | None = None, *, timezone: str | None = None,
             on_failure: str | None = None, max_attempts: int = 3):
    """Declare a scheduled durable workflow. The wrapped run binds the per-run
    `hub` proxy and takes only the hub name (never secrets)."""
    _require_init()

    def deco(fn: Callable):
        name = fn.__name__

        @_DBOS.workflow(name=name)
        def wrapped(hub_name: str | None = None):
            hub_name = hub_name or _HUB_NAME
            with context.run_scope(
                hub=hub_name, workflow=name, hub_dir=_HUB_DIR, engine=_ENGINE,
                settings=_load_settings(), subject=f"workflow:{name}",
            ):
                return fn()

        _REGISTRY[name] = WorkflowDef(
            name=name, fn=fn, wrapped=wrapped, schedule=schedule,
            timezone=timezone, on_failure=on_failure, max_attempts=max_attempts,
        )
        log.info("workflows: registered %r (schedule=%r)", name, schedule)
        return wrapped

    return deco


def step(fn: Callable):
    """A durable step for the author's own side effects (posting, emailing).
    At-least-once — make it idempotent."""
    _require_init()
    return _DBOS.step()(fn)


def launch() -> None:
    global _LAUNCHED
    if _LAUNCHED:
        return
    _DBOS.launch()
    _LAUNCHED = True
    log.info("workflows: DBOS launched (%d workflow(s))", len(_REGISTRY))


def registry() -> list[WorkflowDef]:
    return list(_REGISTRY.values())


def start(name: str, hub_name: str | None = None):
    """Fire one workflow now (used by the dispatcher and `hubzoid schedule run`)."""
    wf = _REGISTRY[name]
    return _DBOS.start_workflow(wf.wrapped, hub_name or _HUB_NAME)


def run_now(name: str, hub_name: str | None = None):
    """Fire and wait — for a one-off manual test."""
    return start(name, hub_name).get_result()


def tick(*, last: datetime, now: datetime | None = None) -> list[str]:
    """Dispatcher step: start every scheduled workflow due in (last, now].
    Returns the names started. Missed fires are skipped (not back-filled)."""
    from .schedule_grammar import due_between

    now = now or datetime.now(timezone.utc)
    started: list[str] = []
    for wf in _REGISTRY.values():
        if not wf.schedule:
            continue
        try:
            if due_between(wf.schedule, wf.timezone, last, now):
                start(wf.name)
                started.append(wf.name)
        except Exception:  # noqa: BLE001 — one bad schedule never stalls the loop
            log.exception("workflows: dispatch failed for %r", wf.name)
    return started


def load_workflows(hub_dir) -> int:
    """Import every workflow module under `<hub>/workflows/<name>/`. A workflow
    is any .py there that applies @workflow. Returns how many were registered."""
    _require_init()
    from .._fs import resolve_bucket

    root = resolve_bucket(Path(hub_dir), "workflows")
    if root is None or not root.is_dir():
        return 0
    before = len(_REGISTRY)
    for wf_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for py in sorted(wf_dir.glob("*.py")):
            if py.name.startswith("_"):
                continue
            mod_name = f"_hubzoid_wf_{wf_dir.name}_{py.stem}"
            spec = importlib.util.spec_from_file_location(mod_name, py)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                try:
                    spec.loader.exec_module(module)
                except Exception:  # noqa: BLE001
                    log.exception("workflows: failed to load %s", py)
    return len(_REGISTRY) - before
