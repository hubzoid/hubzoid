# Hubzoid workflows. Apache-2.0 licensed like the rest of the repository.
"""The DBOS execution layer: `@workflow`, `@step`, and the in-zone dispatcher.

DBOS is the durable-execution engine, embedded (no server). It is invisible to
the author, who only writes `@workflow` / `@step` / `hub`. DBOS owns execution history in a per-hub SQLite database (or shared Postgres);
access, workflow state and configuration share the deployment operational store.

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
from datetime import datetime, timedelta, timezone
from datetime import timezone as utc_timezone
from pathlib import Path
from typing import Any, Callable

from .. import db
from . import context

log = logging.getLogger("hubzoid.workflows")

_DBOS = None  # the DBOS class, imported lazily
_INITED = False
_LAUNCHED = False
_HUB_DIR: Path | None = None
_HUB_NAME: str = ""
_ENGINE: Any = None
_QUEUE = None  # code workflows: one run at a time per workflow, optional hub cap
_MD_QUEUE = None  # markdown schedule tasks + scheduled evals: one at a time per hub
_OWNER = None
_APP_VERSION: str | None = None  # this process's workflow-code version
_lock = threading.Lock()


@dataclass
class WorkflowDef:
    name: str
    fn: Callable
    wrapped: Callable  # the DBOS-wrapped callable
    schedule: str | None
    timezone: str | None
    on_failure: str | None
    run_as: str | None = None
    on_webhook: str | None = None
    concurrency: int | None = None
    concurrency_key: str | None = None
    timeout: float | str | None = None
    alert_to: list | None = None
    queue: Any = None


_REGISTRY: "dict[str, WorkflowDef]" = {}
_IDENTITY_STEP = None  # the checkpointed "who does this run act as" step


def _app_name(hub_name: str) -> str:
    """DBOS app name: 3–30 chars, lowercase, alnum + hyphen."""
    slug = re.sub(r"[^a-z0-9-]", "-", hub_name.lower()).strip("-") or "hub"
    import hashlib

    # Keep names distinct even when punctuation or long prefixes collide.
    slug = f"hz-{slug[:17]}-{hashlib.sha256(hub_name.encode()).hexdigest()[:8]}"
    if len(slug) < 3:
        slug = (slug + "-hub")[:30]
    return slug


def _workflow_code_version(hub_dir: Path, app_name: str) -> str:
    """Identify the hub's workflow code for DBOS recovery.

    DBOS resumes an interrupted run only under the same application version. Its
    default version hashes the registered functions, which here is Hubzoid's own
    wrapper, identical for every hub and every edit. So the version is a hash of
    the installed Hubzoid version plus the hub's workflows/**/*.py: editing a
    workflow or upgrading Hubzoid never replays an old run on different code.
    Knowledge, settings.yaml and config edits don't change it; code a workflow
    imports from outside workflows/ isn't covered. The DBOS app name is mixed in
    because DBOS 3 requires version names to be unique across the apps sharing
    one system database (hubs can share a Postgres one)."""
    import hashlib
    from importlib.metadata import PackageNotFoundError, version

    from .._fs import resolve_bucket

    digest = hashlib.sha256(app_name.encode() + b"\0")
    try:
        digest.update(b"hubzoid " + version("hubzoid").encode() + b"\0")
    except PackageNotFoundError:
        pass
    root = resolve_bucket(Path(hub_dir), "workflows")
    if root is not None and root.is_dir():
        for py in sorted(root.rglob("*.py")):
            if "__pycache__" in py.parts:
                continue
            digest.update(py.relative_to(root).as_posix().encode() + b"\0")
            digest.update(py.read_bytes() + b"\0")
    return "wf-" + digest.hexdigest()[:16]


MIN_SQLITE_FOR_PY312 = (3, 42, 0)


def sqlite_problem(url: str) -> str | None:
    """Why the workflow engine cannot run on this SQLite, or None.

    DBOS 3 on Python 3.12+ stamps rows with `unixepoch('subsec')`, which needs
    SQLite 3.42. Older SQLite returns NULL there and the engine fails to start.
    Python builds that link an old system SQLite (Debian 12's, Ubuntu 22.04's)
    hit this; python.org, uv and Homebrew builds, Debian 13 and Ubuntu 24.04
    ship a newer one. PostgreSQL is unaffected."""
    import sqlite3
    import sys

    if not url.startswith("sqlite") or sys.version_info < (3, 12):
        return None
    have = tuple(int(x) for x in sqlite3.sqlite_version.split(".")[:3])
    if have >= MIN_SQLITE_FOR_PY312:
        return None
    return (f"this Python uses SQLite {sqlite3.sqlite_version}, and the workflow engine needs "
            "SQLite 3.42 or newer on Python 3.12. Scheduled tasks and workflows cannot run. "
            "Use a Python build with a newer SQLite (python.org, uv, Homebrew, Debian 13, "
            "Ubuntu 24.04) or PostgreSQL.")


def init(hub_dir, hub_name: str | None = None) -> None:
    """Construct the DBOS singleton over this hub's database. Idempotent. Must
    run before any workflow module is imported (the decorator needs DBOS)."""
    global _DBOS, _INITED, _HUB_DIR, _HUB_NAME, _ENGINE, _QUEUE, _APP_VERSION, _OWNER
    with _lock:
        if _INITED:
            if _HUB_DIR.resolve() != Path(hub_dir).resolve():
                raise RuntimeError("Each hub needs its own workflow bridge process")
            return
        from dbos import DBOS

        problem = sqlite_problem(db.dbos_url(Path(hub_dir)))
        if problem:
            raise RuntimeError(problem[0].upper() + problem[1:])
        _DBOS = DBOS
        _HUB_DIR = Path(hub_dir)
        _HUB_NAME = hub_name or _HUB_DIR.name
        # hub.state lives in the SHARED operational DB (keyed (hub, workflow, key),
        # collision-safe); DBOS system tables stay PER-BRIDGE so a gateway's N
        # bridges never share one SQLite DBOS system database.
        _ENGINE = db.operational_engine(_HUB_DIR)
        _APP_VERSION = _workflow_code_version(_HUB_DIR, _app_name(_HUB_NAME))
        from .ownership import Owner
        _OWNER = Owner(_HUB_DIR, _HUB_NAME).acquire()
        lost_owner = _OWNER
        _OWNER.on_lost(lambda _reason: _stop_after_owner_loss(lost_owner))
        try:
            DBOS(
                config={
                    "name": _app_name(_HUB_NAME),
                    "system_database_url": db.dbos_url(_HUB_DIR),
                    "application_version": _APP_VERSION,
                    "executor_id": "hub:" + _app_name(_HUB_NAME),
                    "max_executor_threads": int(_load_settings().get("max_executor_threads", 32)),
                }
            )
            # Markdown schedule tasks and scheduled evals run on this same engine.
            from . import markdown

            markdown.register(DBOS, _HUB_DIR, _HUB_NAME)
            from . import events
            events.register(DBOS, _HUB_DIR, _HUB_NAME)
            from . import alerts
            alerts.register(DBOS, _HUB_DIR)
        except BaseException:
            _OWNER.close()
            _OWNER = None
            raise
        _INITED = True
        log.info("workflows: DBOS initialised for hub %r", _HUB_NAME)


def _stop_after_owner_loss(owner) -> None:
    """Stop claiming work the moment ownership is lost, without touching chat.

    `DBOS.destroy` (through `shutdown`) first stops the queue and scheduler
    threads, so no further run is dequeued. Runs already claimed meet an
    ownership guard and raise OwnershipLost, which DBOS does not record, so they
    stay PENDING and the next owner's startup recovery queues them again. The
    bridge's supervisor (boot.py) then retries ownership. Runs in a thread: the
    caller may be a workflow thread that DBOS's teardown waits on. Bound to the
    lost owner, so a late teardown never stops an engine that took over since."""
    threading.Thread(target=shutdown, kwargs={"completion_timeout_sec": 0, "only_for": owner},
                     daemon=True, name="hubzoid-owner-lost").start()


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
    from .events import settings
    return settings(_HUB_DIR)


def _identity_step():
    """The checkpointed step that decides who a run acts as. Its output is the
    run's identity for good: recovery and retries replay it instead of reading
    configuration again, so a run never switches person midway."""
    global _IDENTITY_STEP
    if _IDENTITY_STEP is None:

        @_DBOS.step(name="hz_run_identity")
        def resolve_identity(hub_dir_str: str, hub: str, run_as: str | None,
                             legacy_subject: str, what: str) -> dict:
            from .identity import resolve

            ident = resolve(Path(hub_dir_str), hub=hub, run_as=run_as,
                            legacy_subject=legacy_subject, what=what)
            return ident.to_dict()

        _IDENTITY_STEP = resolve_identity
    return _IDENTITY_STEP


def workflow(
    schedule: str | None = None,
    *,
    timezone: str | None = None,
    on_failure: str | None = None,
    run_as: str | None = None,
    on_webhook: str | None = None,
    concurrency: int | None = None,
    concurrency_key: str | None = None,
    timeout: float | str | None = None,
    alert_to: list | None = None,
):
    """Declare a scheduled durable workflow. The wrapped run binds the per-run
    `hub` proxy and takes only the hub name (never secrets). Retries are a
    per-`@step` concern (`@step(max_attempts=N)`), not a workflow-level knob;
    agent calls retry only with `agent_max_attempts` in workflows/settings.yaml.

    `run_as` names the account the run acts as (see `workflows.identity`);
    without it, the hub or deployment HUBZOID_WORKFLOW_USER, then the setup
    default. It selects an identity and grants nothing."""
    _require_init()
    if run_as is not None:
        from .identity import validate_run_as

        run_as = validate_run_as(run_as)

    if concurrency is not None and (isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1):
        raise ValueError("concurrency must be a positive integer")
    if on_webhook:
        from .events import declarations
        if on_webhook not in declarations(_HUB_DIR):
            raise ValueError(f"Webhook {on_webhook!r} is not configured")
        if any(w.on_webhook == on_webhook for w in _REGISTRY.values()):
            raise ValueError("Only one workflow may consume a named webhook")

    from .deadlines import seconds
    seconds(timeout)
    from .events import validate_destinations
    validate_destinations(alert_to)

    def deco(fn: Callable):
        name = fn.__name__
        if name in _REGISTRY:
            raise ValueError(f"duplicate workflow name: {name}")
        if schedule:
            from .schedule_grammar import next_after

            next_after(schedule, timezone, datetime.now(utc_timezone.utc))
        identity_step = _identity_step()
        # The owner this definition was loaded under. A later owner re-imports
        # the module, so a run claimed by a process that lost ownership keeps
        # failing its guard instead of reading a newer process-global owner.
        owner = _OWNER

        @_DBOS.step(name="hz_deadline_" + name)
        def run_deadline():
            import time
            setting = timeout if timeout is not None else _load_settings().get("workflow_timeout", "15m" if on_webhook else None)
            duration = seconds(setting)
            return time.time() + duration if duration is not None else None

        @_DBOS.workflow(name=name)
        def wrapped(hub_name: str | None = None, event: dict | None = None):
            if owner is not None:
                owner.assert_owned()
            hub_name = hub_name or _HUB_NAME
            identity = identity_step(str(_HUB_DIR), hub_name.lower(), run_as,
                                     f"workflow:{name}", f"Workflow {name!r}")
            from .deadlines import scope
            deadline = run_deadline()
            with scope(absolute=deadline, check_exit=False), context.run_scope(
                hub=hub_name,
                workflow=name,
                hub_dir=_HUB_DIR,
                engine=_ENGINE,
                settings=_load_settings(),
                identity=identity,
                run_id=_DBOS.workflow_id or "",
                event=event, owner=owner,
            ):
                from .alerts import track_run
                with track_run(_HUB_DIR, hub_name, name, _DBOS.workflow_id, deadline):
                    return fn()


        _REGISTRY[name] = WorkflowDef(
            name=name,
            fn=fn,
            wrapped=wrapped,
            schedule=schedule,
            timezone=timezone,
            on_failure=on_failure,
            run_as=run_as, on_webhook=on_webhook, concurrency=concurrency,
            concurrency_key=concurrency_key, timeout=timeout, alert_to=alert_to,
        )
        log.info("workflows: registered %r (schedule=%r)", name, schedule)
        return wrapped

    return deco


def step(fn: Callable | None = None, *, max_attempts: int = 1):
    """A durable step for the author's own side effects (posting, emailing).
    At-least-once — make it idempotent. Opt into retries with
    `@step(max_attempts=N)` (default 1 = no retry)."""
    _require_init()

    def wrap(f: Callable):
        from functools import wraps
        import inspect

        def check():
            from .deadlines import remaining
            remaining()
            ctx = context._run.get()
            owner = (ctx.owner if ctx else None) or _OWNER
            if owner is not None:
                owner.assert_owned()

        if inspect.iscoroutinefunction(f):
            @wraps(f)
            async def checked(*args, **kwargs):
                check()
                return await f(*args, **kwargs)
        else:
            @wraps(f)
            def checked(*args, **kwargs):
                check()
                return f(*args, **kwargs)
        if max_attempts and max_attempts > 1:
            return _DBOS.step(retries_allowed=True, max_attempts=max_attempts)(checked)
        return _DBOS.step()(checked)

    return wrap(fn) if fn is not None else wrap


def _agent_max_attempts() -> int:
    """How many times a failed `hub.call_agent` step is tried.

    It runs the hub's full agent, tools included, so a retry can repeat a
    message or write the first attempt already made. Default 1 (no retry). A
    hub whose agent calls are safe to repeat opts in with
    `agent_max_attempts: N` in workflows/settings.yaml."""
    try:
        raw = _load_settings().get("agent_max_attempts", 1)
        return max(1, int(raw))
    except Exception:  # noqa: BLE001 — a bad value falls back to the safe default
        log.warning("workflows: invalid agent_max_attempts; using 1 (no retry)")
        return 1


def _hub_workflow_cap() -> int | None:
    """Optional limit on code workflows running at once in this hub (none by
    default): `max_concurrent_workflows: N` in workflows/settings.yaml."""
    try:
        raw = _load_settings().get("max_concurrent_workflows")
        return max(1, int(raw)) if raw is not None else None
    except Exception:  # noqa: BLE001 — a bad value means no cap, loudly
        log.warning("workflows: invalid max_concurrent_workflows; no hub cap")
        return None


def _seam_step():
    attempts = _agent_max_attempts()
    if attempts > 1:
        return _DBOS.step(retries_allowed=True, max_attempts=attempts)
    return _DBOS.step()


def _wrap_seams_as_steps() -> None:
    """Wrap the configured seams in DBOS steps so a completed call is
    checkpointed and NOT re-invoked on recovery. Arguments and results are
    plain data (a spec dict in, a result dict out), so DBOS can store them.

    `call_llm` has no side effects, so a failed call is retried once.
    `call_jev` retries rate limits, server errors and timeouts itself (jev.py)
    and fails at once on a bad key or bad questions, so its step adds no retry.
    `call_agent` runs tools, so it is retried only when the hub opts in
    (see `_agent_max_attempts`). An interrupted step can still re-run on
    recovery (at-least-once)."""
    raw_llm, raw_agent, raw_jev = context._LLM, context._AGENT, context._JEV
    if raw_llm is not None and context._LLM_STEP is None:

        @_DBOS.step(retries_allowed=True, max_attempts=2)
        def _llm_step(spec: dict, hub_dir_str: str, subject: str):
            return raw_llm(spec, hub_dir=Path(hub_dir_str), subject=subject)

        context._LLM_STEP = _llm_step
    if raw_agent is not None and context._AGENT_STEP is None:

        @_seam_step()
        def _agent_step(task: str, hub_dir_str: str, subject: str):
            return raw_agent(task, hub_dir=Path(hub_dir_str), subject=subject)

        context._AGENT_STEP = _agent_step
    if raw_jev is not None and context._JEV_STEP is None:

        @_DBOS.step()
        def _jev_step(spec: dict, hub_dir_str: str, subject: str):
            return raw_jev(spec, hub_dir=Path(hub_dir_str), subject=subject)

        context._JEV_STEP = _jev_step


def _step_key() -> str | None:
    """A key unique to the running step of the running workflow, stable across
    recovery (DBOS re-runs an interrupted step with the same id)."""
    wid, sid = _DBOS.workflow_id, _DBOS.step_id
    return f"{wid}:{sid}" if wid and sid is not None else None


def _wrap_delivery_steps() -> None:
    """`hub.publish_artifact` and `hub.send_email` as checkpointed steps. A
    completed step is replayed from its checkpoint; one interrupted mid-way is
    re-run with the same key, which returns the artifact or delivery already
    recorded (and never resends an ambiguous email). No DBOS-level retry: the
    email sender retries only what cannot have been delivered."""
    if context._PUBLISH_STEP is None:

        @_DBOS.step(name="hz_publish_artifact")
        def _publish_step(hub_dir: str, hub: str, identity: dict, workflow: str,
                          run_id: str, request: dict) -> dict:
            return context.publish_now(hub_dir, hub, identity, workflow, run_id, request,
                                       idem_key=_step_key())

        context._PUBLISH_STEP = _publish_step
    if context._EMAIL_STEP is None:

        @_DBOS.step(name="hz_send_email")
        def _email_step(hub_dir: str, hub: str, identity: dict, workflow: str,
                        run_id: str, request: dict) -> dict:
            return context.email_now(hub_dir, hub, identity, workflow, run_id, request,
                                     idem_key=_step_key())

        context._EMAIL_STEP = _email_step


def launch(*, target: str | None = None) -> None:
    global _LAUNCHED
    if _LAUNCHED:
        return
    global _QUEUE, _MD_QUEUE
    _wrap_seams_as_steps()
    _wrap_delivery_steps()
    manual_queue = f"{_app_name(_HUB_NAME)}-manual-{target.replace(chr(58), chr(45))}" if target else None
    if manual_queue:
        _DBOS.listen_queues([manual_queue])
    _DBOS.launch()
    # DBOS 3 persists queue config in the system database, so queues are
    # registered once that exists.
    # Code workflows: one run at a time PER WORKFLOW (a partition per workflow
    # name), so a long workflow never starves an unrelated one. An optional
    # hub-wide cap comes from `max_concurrent_workflows` in workflows/settings.yaml.
    _QUEUE = _DBOS.register_queue(
        f"{_app_name(_HUB_NAME)}-wf",
        global_concurrency=_hub_workflow_cap(),
        partition_concurrency=1,
        on_conflict="always_update",
    )
    # Markdown tasks keep their historical rule: one run at a time per hub, which
    # also keeps git commits/pushes of different tasks from overlapping.
    _MD_QUEUE = _DBOS.register_queue(
        f"{_app_name(_HUB_NAME)}-md", global_concurrency=1, on_conflict="always_update"
    )
    for wf in _REGISTRY.values():
        if wf.concurrency is not None or wf.concurrency_key:
            wf.queue = _DBOS.register_queue(
                f"{_app_name(_HUB_NAME)}-wf-{wf.name}",
                global_concurrency=wf.concurrency or 1,
                partition_concurrency=1 if wf.concurrency_key else None,
                on_conflict="always_update")
        else:
            wf.queue = _QUEUE
    _DBOS.register_queue(f"{_app_name(_HUB_NAME)}-events",
                         on_conflict="always_update")
    _DBOS.register_queue(f"{_app_name(_HUB_NAME)}-alerts", worker_concurrency=4,
                         on_conflict="always_update")
    if target:
        selected = _DBOS.register_queue(manual_queue, global_concurrency=1,
            partition_concurrency=1 if not target.startswith("md:") and _REGISTRY[target].concurrency_key else None,
            on_conflict="always_update")
        if target.startswith("md:"):
            _MD_QUEUE = selected
        else:
            _REGISTRY[target].queue = selected
    else:
        _cancel_runs_from_other_code()
    _LAUNCHED = True
    log.info("workflows: DBOS launched (%d workflow(s))", len(_REGISTRY))
    # Publish this hub's workflow catalog to the shared store so the org portal
    # (served by one bridge) can list every hub's workflows.
    try:
        from ..access import store_for

        store_for(_HUB_DIR).publish_workflows(
            _HUB_NAME,
            [(w.name, w.schedule, w.timezone) for w in _REGISTRY.values()],
        )
    except Exception:  # noqa: BLE001 — catalog publish is best-effort
        log.exception("workflows: could not publish catalog")


def _cancel_runs_from_other_code() -> None:
    """Cancel queued or interrupted runs started by different workflow code.

    DBOS never resumes them under a new application version, but a pending run
    still holds the queue's single slot, so one run interrupted before a code
    change or upgrade would block every later run. Cancelled runs stay in the
    run list with status CANCELLED.

    A queued markdown run is re-queued before it is cancelled, never after: a
    crash or error in between leaves the old run queued (this code never
    dequeues it), so the next start finds it and tries again."""
    try:
        stale = [
            w
            for w in _DBOS.list_workflows(
                status=["PENDING", "ENQUEUED", "DELAYED"], application_name=_app_name(_HUB_NAME)
            )
            if w.app_version is not None and w.app_version != _APP_VERSION
        ]
    except Exception:  # noqa: BLE001 — never block startup on the sweep
        log.exception("workflows: could not list runs from previous code")
        return
    for w in stale:
        if not _requeue_markdown(w):
            continue
        try:
            _DBOS.cancel_workflow(w.workflow_id)
            log.warning(
                "workflows: cancelled run %s of %r (%s under previous workflow code)",
                w.workflow_id, w.name, w.status,
            )
        except Exception:  # noqa: BLE001
            log.exception("workflows: could not cancel stale run %s", w.workflow_id)


def _requeue_markdown(w) -> bool:
    """A markdown task or eval suite that was queued but never started under the
    previous code is queued again under the current code, so its slot is not
    lost (the scheduler already stamped it as fired). One that was interrupted
    mid-run is not repeated, as before DBOS.

    The replacement's id is derived from the old run's, so queueing it again
    after a crash is a no-op. Returns False only when a replacement was needed
    and could not be queued: the old run must then stay as it is."""
    from . import markdown

    if w.status != "ENQUEUED" or w.name not in (markdown.MD_WORKFLOW, markdown.EVAL_WORKFLOW):
        return True
    try:
        args = list((w.input or {}).get("args") or [])
        from dbos import SetWorkflowID

        fn = markdown._FNS["md_task" if w.name == markdown.MD_WORKFLOW else "eval_suite"]
        with SetWorkflowID(f"{w.workflow_id}:requeued"):
            _MD_QUEUE.enqueue(fn, *args)
        log.warning("workflows: re-queued %s under the current code", w.workflow_id)
        return True
    except Exception:  # noqa: BLE001
        log.exception("workflows: could not re-queue %s; left queued for the next start",
                      w.workflow_id)
        return False


def registry() -> list[WorkflowDef]:
    return list(_REGISTRY.values())


def shutdown(*, completion_timeout_sec: float = 5.0, only_for=None) -> None:
    """Tear the DBOS engine down cleanly at bridge shutdown. In-flight runs get
    up to `completion_timeout_sec` to finish; anything still running stays
    recoverable in the system DB and resumes on the next launch (durability is
    preserved — we do NOT cancel recoverable work). Best-effort: a failure here
    must never hang or crash the shutdown path. Resets module state so the
    process could re-init a hub afterwards."""
    global _DBOS, _INITED, _LAUNCHED, _QUEUE, _REGISTRY, _HUB_DIR, _HUB_NAME, _ENGINE, _OWNER
    global _APP_VERSION, _MD_QUEUE, _IDENTITY_STEP
    with _lock:
        if not _INITED or (only_for is not None and _OWNER is not only_for):
            return
        workers = [t for t in threading.enumerate() if t.name.startswith("dbos-executor-")]
        try:
            if _DBOS is not None:
                _DBOS.destroy(workflow_completion_timeout_sec=completion_timeout_sec)
        except Exception:  # noqa: BLE001 — shutdown must be safe
            log.exception("workflows: DBOS shutdown failed (continuing)")
        finally:
            if _OWNER is not None:
                owner, _OWNER = _OWNER, None
                try:
                    owner.retire()
                except Exception:  # noqa: BLE001 — the lease then lapses on its own
                    log.exception("workflows: could not retire the owner lease")
                alive = [t for t in workers if t.is_alive()]
                if alive:
                    # DBOS.destroy does not join timed-out synchronous work.
                    # Keep exclusion until every old executor thread exits.
                    def release_after_exit():
                        for worker in alive:
                            worker.join()
                        owner.close()
                    threading.Thread(target=release_after_exit, daemon=True,
                                     name="hubzoid-owner-drain").start()
                else:
                    owner.close()
            _DBOS = None
            _INITED = False
            _LAUNCHED = False
            _QUEUE = None
            _MD_QUEUE = None
            _REGISTRY = {}
            _HUB_DIR = None
            _HUB_NAME = None
            _ENGINE = None
            _APP_VERSION = None
            _IDENTITY_STEP = None
            context._PUBLISH_STEP = None
            context._EMAIL_STEP = None

            # Rebuilt on the next launch, with that hub's retry setting.
            context._LLM_STEP = None
            context._AGENT_STEP = None
            context._JEV_STEP = None
            log.info("workflows: DBOS shut down")


def start(name: str, hub_name: str | None = None, *, scheduled_at=None):
    """Fire one workflow now (dispatcher + `hubzoid schedule run`). Enqueues on
    the hub's workflow queue in this workflow's partition, so two runs of the
    same workflow never overlap while different workflows run side by side."""
    wf = _REGISTRY[name]
    if _OWNER is not None:
        from .ownership import OwnershipLost
        try:
            _OWNER.assert_owned()
        except OwnershipLost as exc:
            # Callers here are operators and the dispatcher, not a run.
            raise RuntimeError(str(exc)) from None
    import uuid
    key = f"{hub_name or _HUB_NAME}:{name}:{scheduled_at}" if scheduled_at else None
    if _QUEUE is not None:
        options = enqueue_options(wf)
        options.update(workflow_name=name, attributes={"trigger": "schedule" if scheduled_at else "manual"})
        if key:
            options["workflow_id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, key))
        return _DBOS.enqueue_workflow_with_options(options, hub_name or _HUB_NAME)
    return _DBOS.start_workflow(wf.wrapped, hub_name or _HUB_NAME)


def run_now(name: str, hub_name: str | None = None):
    """Fire and wait — for a one-off manual test."""
    return start(name, hub_name).get_result()


MISSED_LOG_DAYS = 31


def missed_log(health: dict, count: int, now: datetime) -> list:
    """The hub's dated record of skipped scheduled slots, for runtime health:
    `[utc_iso, count]` pairs from the last MISSED_LOG_DAYS days, plus one for
    `count` slots skipped at `now` when there are any. The Console reads it."""
    now = now.astimezone(timezone.utc)
    cutoff = now - timedelta(days=MISSED_LOG_DAYS)
    kept = []
    for entry in health.get("missed_log") or []:
        try:
            at = datetime.fromisoformat(entry[0])
            if (at if at.tzinfo else at.replace(tzinfo=timezone.utc)) >= cutoff:
                kept.append([entry[0], int(entry[1])])
        except (TypeError, ValueError, IndexError):
            continue  # a malformed entry is dropped, never fatal
    if count:
        kept.append([now.isoformat(), int(count)])
    return kept


def tick(*, last: datetime, now: datetime | None = None) -> list[str] | None:
    """Dispatcher step: start every scheduled workflow due in (last, now].
    Returns the names started, or None while a backup holds new runs (the
    caller keeps `last`, so a slot inside the hold fires when it ends; runs
    already started in this tick are not repeated, their ids are per slot).
    Missed fires are skipped (not back-filled)."""
    from .schedule_grammar import due_between

    now = now or datetime.now(timezone.utc)
    started: list[str] = []
    failed: list[str] = []
    from ..access import store_for

    gs = store_for(_HUB_DIR)
    if gs.schedule_hold():
        return None
    paused = gs.paused_workflows(_HUB_NAME)
    for wf in _REGISTRY.values():
        if not wf.schedule or wf.name in paused:
            continue
        try:
            if due_between(wf.schedule, wf.timezone, last, now):
                from .schedule_grammar import next_after

                due = next_after(wf.schedule, wf.timezone, last)
                missed = 0
                # Keep only the latest slot when a dispatcher is delayed.
                while True:
                    following = next_after(wf.schedule, wf.timezone, due)
                    if following > now:
                        break
                    due, missed = following, missed + 1
                # A backup or a pause can begin while earlier workflows in this
                # tick start, so both are read again right before this one.
                if gs.schedule_hold():
                    return None
                if wf.name in gs.paused_workflows(_HUB_NAME):
                    continue
                start(wf.name, scheduled_at=due.astimezone(timezone.utc).isoformat())
                health = gs.runtime_health(_HUB_NAME)
                gs.set_runtime_health(
                    _HUB_NAME,
                    last_dispatch=now.isoformat(),
                    missed=health.get("missed", 0) + missed,
                    missed_log=missed_log(health, missed, now),
                )
                started.append(wf.name)
        except Exception:  # noqa: BLE001 — one bad schedule never stalls the loop
            failed.append(wf.name)
            log.exception("workflows: dispatch failed for %r", wf.name)
    if failed:
        raise RuntimeError("Could not dispatch workflows: " + ", ".join(failed))
    return started


def downtime_missed(since: datetime, now: datetime | None = None) -> dict:
    """Count scheduled slots that fell in (since, now] while no dispatcher was
    running (e.g. across a bridge restart). Reporting only — the dispatcher does
    NOT back-fill these; this just makes the downtime window visible."""
    from .schedule_grammar import next_after

    now = now or datetime.now(timezone.utc)
    total = 0
    per: dict[str, int] = {}
    for wf in _REGISTRY.values():
        if not wf.schedule:
            continue
        count = 0
        cursor = since
        # Cap the walk so a pathological schedule + long downtime can't spin.
        for _ in range(100_000):
            cursor = next_after(wf.schedule, wf.timezone, cursor)
            if cursor > now:
                break
            count += 1
        if count:
            per[wf.name] = count
            total += count
    return {
        "since": since.astimezone(timezone.utc).isoformat(),
        "until": now.astimezone(timezone.utc).isoformat(),
        "missed": total,
        "by_workflow": per,
    }


def load_workflows(hub_dir) -> int:
    """Import every workflow module under `<hub>/workflows/<name>/`. A workflow
    is any .py there that applies @workflow. Returns how many were registered."""
    _require_init()
    from .._fs import resolve_bucket

    root = resolve_bucket(Path(hub_dir), "workflows")
    if root is None or not root.is_dir():
        return 0
    from .observe import definitions

    errors = [r for r in definitions(hub_dir) if r["error"]]
    if errors:
        raise ValueError(str(errors))
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
                except Exception:
                    log.exception("workflows: failed to load %s", py)
                    raise
    return len(_REGISTRY) - before


def enqueue_options(wf, event=None):
    """One queue/partition policy for schedules, operator runs and events."""
    dedicated = wf.concurrency is not None or bool(wf.concurrency_key)
    queue_name = f"{_app_name(_HUB_NAME)}-wf" + (f"-{wf.name}" if dedicated else "")
    options = {"queue_name": wf.queue.name if wf.queue is not None else queue_name}
    if wf.concurrency_key:
        from .webhooks import field
        import json
        value = field((event or {}).get("body", {}), wf.concurrency_key)
        options["queue_partition_key"] = json.dumps(value, sort_keys=True) if value is not None else "missing"
    elif not dedicated:
        options["queue_partition_key"] = wf.name
    return options


def ready_record():
    return {"version": _APP_VERSION,
            "webhooks": {w.on_webhook: w.name for w in _REGISTRY.values() if w.on_webhook},
            "definitions": {w.name: {"queue": enqueue_options(w)["queue_name"],
                                      "manual_partition": enqueue_options(w).get("queue_partition_key")}
                            for w in _REGISTRY.values()}}
