# Hubzoid workflows. Apache-2.0 licensed like the rest of the repository.
"""Run controls for one hub's workflows, shared by the CLI and the agent tools.

One code path per action: list, history, start now, pause or resume a schedule,
cancel a run. The caller says who acts (`actor`) and from where (`surface`);
this module never decides who may act. Authorization happens before it is
called (the access guard for tools, the server account for the CLI).

A run always acts as the workflow's own account (`workflows.identity`), never
as the person who started it, and starting a run does not let that person read
its results (`observe.may_see_results`).

Blocking calls throughout: tools call these through `asyncio.to_thread`. No
agent SDK imports here.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import observe

log = logging.getLogger("hubzoid.workflows")

#: DBOS states of a run that is queued or running (cancellable, "already running").
ACTIVE = ("PENDING", "ENQUEUED")
#: Most runs one history read returns.
MAX_RUNS = 25

# Serialises the "already running?" check and the start within this process,
# so two quick requests for the same workflow queue one run, not two.
_start_lock = threading.Lock()


class ControlError(Exception):
    """A run control that cannot be done. `message` is safe to show a person;
    `code` is a stable token: unknown, unknown_run, not_running, code_off,
    broken, held, finished, bad_filter. `status` is the run's state when that
    explains the refusal (a finished run)."""

    def __init__(self, message: str, code: str, *, status: str | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


@dataclass(frozen=True)
class Target:
    name: str               # stored form: `md:<task>` or the code workflow's function name
    kind: str               # "markdown" | "code"
    task: str | None = None  # the markdown task name


def resolve(hub_dir, name: str) -> Target:
    """A task or workflow name as a person or the CLI writes it (`md:x`, `x`,
    hyphens or underscores for a code workflow) to its stored form."""
    from .. import scheduling as sch

    hub_dir = Path(hub_dir)
    raw = (name or "").strip()
    bare = raw[3:] if raw.startswith("md:") else raw
    if bare:
        tasks, _ = sch.load_tasks(hub_dir)
        if any(t.name == bare for t in tasks):
            return Target(f"md:{bare}", "markdown", bare)
        want = bare.replace("-", "_")
        for w in observe.definitions(hub_dir):
            if w["name"] in (bare, want):
                return Target(w["name"], "code")
    raise ControlError(f"There is no workflow or scheduled task named {raw!r} in this agent.",
                       "unknown")


def _last_run(row: dict) -> dict:
    return dict(id=row["id"], status=row["status"], created=row["created"],
                completed=row["completed"])


def overview(hub_dir, *, viewer: str | None) -> list[dict]:
    """`observe.catalog` rows (code workflows, then markdown tasks) with
    `kind` and `last_run` ({id, status, created, completed} or None). A
    history that can't be read leaves `last_run` None; the listing still works."""
    hub_dir = Path(hub_dir)
    rows = observe.catalog(hub_dir)
    for row in rows:
        row.setdefault("kind", "code")
        row["last_run"] = None
        if row["name"] == "md:?":
            continue  # a schedule file that doesn't parse: no task, no runs
        try:
            last = observe.runs(hub_dir, name=row["name"], limit=1, viewer=viewer)
        except Exception:  # noqa: BLE001 — history is extra; the listing stands
            log.warning("workflows: last run of %s in %s unavailable", row["name"],
                        hub_dir.name, exc_info=True)
            continue
        if last:
            row["last_run"] = _last_run(last[0])
    return rows


def history(hub_dir, *, viewer: str | None, workflow: str | None = None,
            run_id: str | None = None, status=None, limit: int = 10) -> list[dict]:
    """Recent runs, newest first, or one run with its steps (`run_id`). Output
    and error text only when `viewer` is the account the run acted as."""
    hub_dir = Path(hub_dir)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = 10
    limit = max(1, min(MAX_RUNS, limit))
    name = resolve(hub_dir, workflow).name if workflow else None
    try:
        statuses = observe.resolve_statuses(status)
    except ValueError:
        raise ControlError("Unknown status. Use running, failed, succeeded or cancelled.",
                           "bad_filter") from None
    return observe.runs(hub_dir, name=name, run_id=run_id or None, statuses=statuses,
                        limit=limit, viewer=viewer)


def _engine_here(hub_dir: Path) -> bool:
    """Whether this process runs the DBOS engine for `hub_dir`."""
    from . import runtime

    here = runtime._HUB_DIR
    return bool(runtime._LAUNCHED and here is not None
                and Path(here).resolve() == Path(hub_dir).resolve())


def _runs_as(hub_dir: Path, target: Target) -> str | None:
    """The account a run of `target` acts as, by the resolution the catalogue
    and the run itself use. Never the caller."""
    for row in observe.catalog(hub_dir):
        if row["name"] == target.name:
            return (row.get("runs_as") or {}).get("account")
    return None


def _check_startable(hub_dir: Path, target: Target) -> None:
    from .. import scheduling as sch
    from . import runtime
    from .boot import schedules_enabled

    if target.kind == "markdown":
        task = next((t for t in sch.load_tasks(hub_dir)[0] if t.name == target.task), None)
        if task is not None and not task.enabled:
            raise ControlError(f"{target.name} is switched off in its file (enabled: false), "
                               "so it can't be started.", "broken")
        return
    definition = next((d for d in observe.definitions(hub_dir) if d["name"] == target.name), None)
    if definition and definition["error"]:
        raise ControlError(f"{target.name} has a problem in its code and can't run until it is "
                           "fixed. The Console's Workflows page shows the error.", "broken")
    if target.name not in runtime._REGISTRY:
        if schedules_enabled():
            raise ControlError(f"{target.name} was not loaded when this agent started. Check "
                               "the server logs, fix the workflow and restart the agent.",
                               "broken")
        raise ControlError("Code workflows are off on this agent (HUBZOID_SCHEDULES is not set).",
                           "code_off")


def _active(target: Target) -> list[str]:
    from . import markdown, runtime

    if target.kind == "markdown":
        return markdown.active_runs(target.task)
    # DBOS scopes a listing without ids to this hub's application.
    return [w.workflow_id for w in runtime._DBOS.list_workflows(
        name=target.name, status=list(ACTIVE), load_input=False, load_output=False)]


def _audit(hub_dir: Path, action: str, target: str, **fields) -> None:
    """Record a run control that already happened. A failed write is logged
    loudly, not raised: the run was started or cancelled either way, and
    saying otherwise would mislead (the access guard's decision row, written
    before the call, still names the person)."""
    from ..access import store_for

    try:
        store_for(hub_dir).audit_run_control(hub_dir.name, action, target, **fields)
    except Exception:  # noqa: BLE001
        log.exception("workflows: could not audit %s of %s in %s", action, target, hub_dir.name)


def start_now(hub_dir, name: str, *, actor: str, surface: str,
              request_id: str | None = None) -> dict:
    """Queue one run of a workflow or markdown task now, on this process's
    engine. Returns {run_id, workflow, kind, runs_as, already_running}. When
    a run is already queued or running, returns that one and records nothing."""
    from ..access import store_for
    from . import markdown, runtime

    hub_dir = Path(hub_dir)
    target = resolve(hub_dir, name)
    if not _engine_here(hub_dir):
        raise ControlError(
            "The workflow engine isn't running in this agent, so runs can't be started here. "
            "It starts with the agent when the agent has scheduled tasks, or code workflows "
            "with schedules on.", "not_running")
    gs = store_for(hub_dir)
    if gs.schedule_hold():
        raise ControlError("New runs are on hold while a backup runs. Try again in a few minutes.",
                           "held")
    _check_startable(hub_dir, target)
    runs_as = _runs_as(hub_dir, target)
    out = dict(workflow=target.name, kind=target.kind, runs_as=runs_as)
    with _start_lock:
        active = _active(target)
        if active:
            return dict(out, run_id=active[0], already_running=True)
        if target.kind == "markdown":
            slot = "manual-" + datetime.now().strftime("%Y%m%dT%H%M%S")
            handle = markdown.enqueue_task(target.task, slot)
        else:
            handle = runtime.start(target.name)
        run_id = handle.get_workflow_id()
    _audit(hub_dir, "run_start", target.name, actor=actor, surface=surface,
           request_id=request_id, subject=run_id)
    log.info("workflows: %s started %s (%s) from %s", actor, target.name, run_id, surface)
    return dict(out, run_id=run_id, already_running=False)


def set_paused(hub_dir, name: str, paused: bool, *, actor: str, surface: str,
               request_id: str | None = None) -> dict:
    """Pause or resume a schedule. Returns {workflow, paused, changed}; the
    audit row is written either way."""
    from ..access import store_for

    hub_dir = Path(hub_dir)
    target = resolve(hub_dir, name)
    gs = store_for(hub_dir)
    changed = (target.name in gs.paused_workflows(hub_dir.name)) != bool(paused)
    gs.set_workflow_paused(hub_dir.name, target.name, bool(paused), actor=actor,
                           surface=surface, request_id=request_id)
    return dict(workflow=target.name, paused=bool(paused), changed=changed)


def cancel(hub_dir, run_id: str, *, actor: str, surface: str,
           request_id: str | None = None) -> dict:
    """Cancel a queued or running run of this hub. Best effort: it stops at
    its next step, and work already done is not undone. Returns {run_id,
    workflow, previous}."""
    hub_dir = Path(hub_dir)
    run_id = (run_id or "").strip()
    # The run must belong to this hub: the listing is scoped to its DBOS
    # application, so another hub's run id sharing the database is not found.
    rows = observe.runs(hub_dir, run_id=run_id, limit=1, trusted=False) if run_id else []
    if not rows:
        raise ControlError(f"There is no run {run_id!r} in this agent.", "unknown_run")
    row = rows[0]
    if row["status"] not in ACTIVE:
        raise ControlError(f"Run {run_id} already finished ({row['status']}), so there is "
                           "nothing to cancel.", "finished", status=row["status"])
    if _engine_here(hub_dir):
        from . import runtime

        runtime._DBOS.cancel_workflow(run_id)
    else:
        from dbos import DBOSClient

        from .. import db
        from .runtime import _app_name

        client = DBOSClient(system_database_url=db.dbos_url(hub_dir),
                            application_name=_app_name(hub_dir.name))
        try:
            client.cancel_workflow(run_id)
        finally:
            client.destroy()
    _audit(hub_dir, "run_cancel", run_id, actor=actor, surface=surface, request_id=request_id)
    log.info("workflows: %s cancelled %s from %s", actor, run_id, surface)
    return dict(run_id=run_id, workflow=row["name"], previous=row["status"])
