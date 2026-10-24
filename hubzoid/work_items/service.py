"""Work-item actions, shared by the CLI, the HTTP routes and the engine.

The caller says who acts (`actor`, a normalized account email or a CLI
operator) and from where (`surface`). Authorization for each action happens
here, so every surface gets the same answer:

* starting work needs the workflow's own grant (`wf_<name>`) or Run and control
  workflows (`workflows_manage`) on the hub;
* the person who asked for the work sees everything about it. A hub manager
  (`workflows_view` or `workflows_manage`) sees its state but not its input,
  result or operation details;
* only the person who asked for the work approves, denies or resolves its
  operations. Being an administrator grants no approval;
* the requester or a hub manager can cancel.

Execution history, stage checkpoints and versions live in the workflow engine.
This module keeps the item and operation rows in step with it.
"""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

from .. import redaction
from . import config as config_lib
from . import engine as engine_lib
from . import operations as ops_lib
from . import store
from .errors import WorkItemError, denied, not_found

log = logging.getLogger("hubzoid.work_items")

GENERATION_MODES = ("reuse", "new")
MAX_INBOX = 1000


# -- helpers ----------------------------------------------------------------

def _hub(hub_dir) -> str:
    return Path(hub_dir).name.lower()


def _norm(subject: str | None) -> str:
    from ..access import normalize

    return normalize(subject or "")


def _gs(hub_dir):
    from ..access import store_for

    return store_for(Path(hub_dir))


def _can(hub_dir, actor: str, permission: str) -> bool:
    if actor.startswith("cli:"):
        return True  # the server account: run controls act with its authority
    try:
        gs = _gs(hub_dir)
        return bool(gs.can(actor, _hub(hub_dir), permission) or gs.can(actor, "*", "manage_access"))
    except Exception:  # noqa: BLE001
        return False


def _audit(hub_dir, actor: str, action: str, subject: str, *, surface: str | None = None,
           request_id: str | None = None) -> None:
    try:
        _gs(hub_dir).audit_event(actor, action, subject=subject, hub=_hub(hub_dir),
                                 surface=surface, request_id=request_id)
    except Exception:  # noqa: BLE001 - the audit is best-effort here; the row is the record
        log.exception("work items: could not write the audit row for %s", action)


def _definition(hub_dir, workflow: str):
    from ..workflows.runtime import definition

    wf = definition(Path(hub_dir), workflow)
    if wf is None:
        raise WorkItemError("route_missing", f"There is no workflow {workflow!r} in this hub.",
                            status=404)
    routes = (wf.extra or {}).get("work_items")
    if not routes:
        raise WorkItemError(
            "invalid_definition",
            f"Workflow {workflow!r} does not run work items. Add work_items=True (or routes) "
            "to its @workflow declaration.", status=422)
    return wf, routes


def input_digest(value) -> str:
    return ops_lib.digest({"input": value})


def requester_of(item: dict) -> str:
    return item.get("requester") or (item.get("origin_json") or {}).get("acts_as") or ""


def _is_requester(item_or_op: dict, actor: str) -> bool:
    who = item_or_op.get("requester") or (item_or_op.get("origin_json") or {}).get("acts_as") or ""
    return bool(who) and _norm(who) == _norm(actor)


# -- admission and start ------------------------------------------------------

def admit(hub_dir, *, workflow: str, key: str, route: str | None = None, input=None,
          requester: str = "", origin: dict | None = None, generation_mode: str = "reuse",
          actor: str | None = None) -> tuple[dict, bool]:
    """Record one piece of requested work. The same key and input returns the
    existing item. Different input under an active key is refused. A new
    generation only when asked for, and only once the previous one finished."""
    hub_dir = Path(hub_dir)
    hub = _hub(hub_dir)
    if generation_mode not in GENERATION_MODES:
        raise WorkItemError("invalid_request", "generation_mode is reuse or new.")
    if not isinstance(key, str) or not key.strip() or len(key) > 512:
        raise WorkItemError("invalid_request", "key is required: a stable id such as operator:demo-1.")
    wf, routes = _definition(hub_dir, workflow)
    route = route or next(iter(routes))
    if route not in routes:
        raise WorkItemError("route_missing",
                            f"Workflow {workflow!r} has no route {route!r}. Routes: {', '.join(routes)}.",
                            status=422)
    if actor is not None and not actor.startswith("cli:"):
        if not (_can(hub_dir, actor, wf.permission) or _can(hub_dir, actor, "workflows_manage")):
            raise denied(f"Starting {workflow} needs its own grant or Run and control workflows.")
    cfg = config_lib.load(hub_dir)
    if cfg.admission_paused:
        raise WorkItemError("admission_paused",
                            "New work is paused for this hub while it is upgraded. Try again later.",
                            status=503, retryable=True)
    encoded = json.dumps(input, sort_keys=True, default=str)
    if len(encoded.encode()) > cfg.max_input_bytes:
        raise WorkItemError("validation_failed",
                            f"The input is larger than {cfg.max_input_bytes} bytes. Pass a file or "
                            "link instead.", status=422)
    digest = input_digest(input)
    with store.locked(hub_dir, f"wi-admit:{hub}") as conn:
        latest = store.latest_for_key(conn, hub, key)
        if latest is not None:
            if generation_mode == "reuse":
                if latest["input_digest"] == digest:
                    return latest, False
                raise WorkItemError(
                    "key_conflict",
                    "This key already has work with different input. Send a follow-up to active "
                    "work, or ask for a new generation once it has finished.",
                    status=409, item_id=latest["id"], current_digest=latest["input_digest"])
            if latest["state"] not in store.TERMINAL:
                raise WorkItemError("key_conflict",
                                    "The current work for this key is still active. Send it a follow-up "
                                    "instead.", status=409, item_id=latest["id"])
        if store.count_active(conn, hub) >= cfg.max_pending:
            raise WorkItemError("backlog_full", "This hub has too much work waiting. Try again later "
                                "with the same key.", status=429, retryable=True)
        item = store.insert_item(
            conn, id=store.new_id(), hub=hub, logical_key=key,
            generation=(latest["generation"] + 1) if latest else 1, workflow=workflow, route=route,
            route_json=routes[route], state="accepted", input_digest=digest,
            input_json={"input": input}, requester=_norm(requester), origin_json=origin or {},
            counters_json={}, inbox_json=[])
    _audit(hub_dir, actor or requester or "system", "work_item_accept", item["id"],
           surface=(origin or {}).get("surface"))
    return item, True


def start(hub_dir, item: dict) -> dict:
    """Queue the first execution of an accepted item on the live engine.

    The execution is recorded on the item before it is queued, so a worker
    that starts at once finds itself expected. Queueing uses a fixed id, so
    repeating it (after a crash between the two) runs the work once."""
    if item["state"] != "accepted" or item["execution"] > 1:
        return item
    wid = engine_lib.execution_id(item["id"], 1)
    if item["execution"] == 0:
        try:
            item = store.update_item(hub_dir, item["id"], expected_revision=item["revision"],
                                     expect_state=("accepted",), execution=1, workflow_id=wid)
        except WorkItemError:
            item = store.require_item(hub_dir, item["id"])
            if item["execution"] != 1 or item["workflow_id"] != wid:
                return item
    with engine_lib.live(hub_dir) as eng:
        options = {"workflow_name": item["workflow"], "queue_name": engine_lib.queue_name(Path(hub_dir).name),
                   "queue_partition_key": item["id"], "workflow_id": wid}
        envelope = {"trigger": "work_item", "scope": "team", "work_item": item["id"],
                    "started_by": item.get("requester") or None}
        eng.enqueue(options, item["hub"], None, envelope)
    return store.require_item(hub_dir, item["id"])


def admit_and_start(hub_dir, **kwargs) -> tuple[dict, bool]:
    item, created = admit(hub_dir, **kwargs)
    if item["state"] == "accepted" and item["execution"] == 0:
        item = start(hub_dir, item)
    return item, created


# -- the engine side ----------------------------------------------------------

def bind(hub_dir, item_id: str, workflow_id: str, app_version: str | None) -> dict | None:
    """Called when an execution begins. Returns the item to run, or None when
    it must not run: cancelled, finished, or not the item's current execution.
    The checks are made again after any concurrent change."""
    for _ in range(10):
        item = store.get_item(hub_dir, item_id)
        if item is None or item["state"] in store.TERMINAL or item["cancel_requested"]:
            return None
        if item.get("workflow_id") != workflow_id:
            return None
        try:
            return store.update_item(hub_dir, item_id, expected_revision=item["revision"],
                                     expect_workflow_id=workflow_id, state="running", reason=None,
                                     app_version=item.get("app_version") or app_version)
        except WorkItemError:
            continue
    return None


def _fenced(hub_dir, item_id: str, workflow_id: str, **fields) -> dict | None:
    """Record an execution's outcome only while it is still the item's
    current, live execution. A cancellation or newer execution wins."""
    for _ in range(10):
        item = store.get_item(hub_dir, item_id)
        if item is None or item["state"] in store.TERMINAL or item.get("workflow_id") != workflow_id:
            return None
        try:
            return store.update_item(hub_dir, item_id, expected_revision=item["revision"],
                                     expect_workflow_id=workflow_id, **fields)
        except WorkItemError:
            continue
    return None


def parked(hub_dir, item_id: str, workflow_id: str, kind: str, operation_id: str,
           reason: str | None = None) -> None:
    op = store.get_op(hub_dir, operation_id) or {}
    _fenced(hub_dir, item_id, workflow_id,
            state="waiting" if kind == "approval" else "needs_input",
            reason=reason or ("approval_required" if kind == "approval" else "operation_ambiguous"),
            resume_step=op.get("resume_step"))


def finished(hub_dir, item_id: str, workflow_id: str, *, result=None,
             error: BaseException | None = None) -> None:
    from .operations import OperationFailed

    cfg = config_lib.load(hub_dir)
    if error is None:
        encoded = json.dumps(result, default=str)
        if len(encoded.encode()) > cfg.max_result_bytes:
            result = {"note": f"The result was larger than {cfg.max_result_bytes} bytes and was not "
                              "stored. Publish large results as a file."}
        _fenced(hub_dir, item_id, workflow_id, state="completed", reason=None,
                result_json={"result": json.loads(json.dumps(result, default=str))})
    elif isinstance(error, (OperationFailed, WorkItemError)):
        _fenced(hub_dir, item_id, workflow_id, state="stopped", reason=error.code,
                error=redaction.scrub_text(error.message))
    else:
        _fenced(hub_dir, item_id, workflow_id, state="failed", reason="stage_failed",
                error=redaction.scrub_error(error))


def definition_changed(hub_dir, workflow_id: str, old_version: str | None, new_version: str | None) -> None:
    """A queued or interrupted execution belongs to code that changed. Work
    never moves silently to new code: the item stops for a person to restart
    it from a saved stage."""
    if not workflow_id.startswith("wi-"):
        return
    item_id = workflow_id[3:].rsplit("-e", 1)[0]
    _fenced(hub_dir, item_id, workflow_id, state="needs_input", reason="definition_changed",
            error=f"The workflow code changed from {old_version} to {new_version} while this "
                  "work was active. Restart it from a saved stage once you have checked it.")


# -- reading --------------------------------------------------------------------

def _may_see(hub_dir, item: dict, viewer: str) -> tuple[bool, bool]:
    """(may see the item at all, may see its content)."""
    if viewer.startswith("cli:") or _is_requester(item, viewer):
        return True, True
    manager = _can(hub_dir, viewer, "workflows_view") or _can(hub_dir, viewer, "workflows_manage")
    return manager, False


def _view(item: dict, *, full: bool) -> dict:
    out = {k: item.get(k) for k in (
        "id", "workflow", "logical_key", "generation", "route", "state", "reason", "requester",
        "execution", "workflow_id", "revision", "created", "updated", "cancel_requested")}
    out["route_detail"] = item.get("route_json")
    out["origin"] = {k: v for k, v in (item.get("origin_json") or {}).items() if k in ("source", "surface")}
    out["error"] = item.get("error")
    if full:
        out["input"] = (item.get("input_json") or {}).get("input")
        out["result"] = (item.get("result_json") or {}).get("result")
        out["pending_messages"] = sum(1 for m in (item.get("inbox_json") or []) if not m.get("consumed"))
    return out


def show(hub_dir, item_id: str, viewer: str) -> dict:
    item = store.require_item(hub_dir, item_id, _hub(hub_dir))
    visible, full = _may_see(hub_dir, item, viewer)
    if not visible:
        raise not_found()
    out = _view(item, full=full)
    stages = []
    if item.get("workflow_id"):
        try:
            with engine_lib.live(hub_dir, require=False) as eng:
                if eng is not None:
                    stages = engine_lib.stage_steps(eng, item["workflow_id"])
        except Exception:  # noqa: BLE001 - stages are a view; the row stays authoritative
            log.exception("work items: could not read stages for %s", item_id)
    out["stages"] = stages
    is_requester = viewer.startswith("cli:") or _is_requester(item, viewer)
    out["operations"] = [ops_lib.view(o, full=is_requester)
                         for o in store.list_ops(hub_dir, item["hub"], item_id=item["id"])]
    return out


def list_items(hub_dir, viewer: str, *, state: str | None = None, workflow: str | None = None,
               before: float | None = None, limit: int = 50, mine: bool = False) -> dict:
    """Work this viewer may see; ``mine``: only the work they asked for, even
    when they manage the hub."""
    hub = _hub(hub_dir)
    if state and state not in store.ITEM_STATES:
        raise WorkItemError("invalid_request", f"state is one of {', '.join(store.ITEM_STATES)}.")
    manager = viewer.startswith("cli:") or _can(hub_dir, viewer, "workflows_view") \
        or _can(hub_dir, viewer, "workflows_manage")
    rows = store.list_items(hub_dir, hub, state=state, workflow=workflow, before=before, limit=limit,
                            requester=None if manager and not mine else _norm(viewer))
    items = [_view(r, full=viewer.startswith("cli:") or _is_requester(r, viewer)) for r in rows]
    nxt = rows[-1]["created"] if len(rows) == max(1, min(int(limit), 100)) else None
    return {"items": items, "next_cursor": nxt}


def list_operations(hub_dir, viewer: str, *, state: str | None = None, limit: int = 100) -> dict:
    hub = _hub(hub_dir)
    if state and state not in store.OP_STATES:
        raise WorkItemError("invalid_request", f"state is one of {', '.join(store.OP_STATES)}.")
    rows = store.list_ops(hub_dir, hub, state=state, limit=limit)
    manager = viewer.startswith("cli:") or _can(hub_dir, viewer, "workflows_view") \
        or _can(hub_dir, viewer, "workflows_manage")
    out = []
    for row in rows:
        mine = viewer.startswith("cli:") or _is_requester(row, viewer)
        if mine or manager:
            out.append(ops_lib.view(row, full=mine))
    return {"operations": out}


def operation_detail(hub_dir, op_id: str, viewer: str) -> dict:
    row = store.get_op(hub_dir, op_id)
    if row is None or row["hub"] != _hub(hub_dir):
        raise not_found("operation")
    mine = viewer.startswith("cli:") or _is_requester(row, viewer)
    if not mine and not (_can(hub_dir, viewer, "workflows_view") or _can(hub_dir, viewer, "workflows_manage")):
        raise not_found("operation")
    out = ops_lib.view(row, full=mine)
    if mine:
        out["payload"] = redaction.redact(ops_lib.payload(hub_dir, row))
    return out


# -- changes --------------------------------------------------------------------

def message(hub_dir, item_id: str, actor: str, text: str, *, client_message_id: str | None = None,
            expected_revision: int | None = None) -> dict:
    """A follow-up for active work. It waits in the item's inbox and the
    workflow reads it at its next stage boundary (`hub.work_item.messages()`)."""
    item = store.require_item(hub_dir, item_id, _hub(hub_dir))
    if not (actor.startswith("cli:") or _is_requester(item, actor)):
        raise not_found()
    if item["state"] in store.TERMINAL:
        raise WorkItemError("not_active", "This work has finished. Start new work for a new request.",
                            status=409)
    if not isinstance(text, str) or not text.strip() or len(text) > 20000:
        raise WorkItemError("validation_failed", "A follow-up is 1 to 20000 characters.", status=422)
    inbox = list(item.get("inbox_json") or [])
    if client_message_id and any(m.get("client_message_id") == client_message_id for m in inbox):
        existing = next(m for m in inbox if m.get("client_message_id") == client_message_id)
        return {"message_id": existing["id"], "revision": item["revision"], "queued": True}
    if sum(1 for m in inbox if not m.get("consumed")) >= MAX_INBOX:
        raise WorkItemError("rate_limited", "Too many unread follow-ups for this work.", status=429)
    entry = {"id": store.new_id(), "from": _norm(actor), "text": text, "at": store.now(),
             "client_message_id": client_message_id, "consumed": False}
    item = store.update_item(hub_dir, item_id,
                             expected_revision=expected_revision if expected_revision is not None else item["revision"],
                             inbox_json=inbox + [entry])
    _audit(hub_dir, actor, "work_item_message", item_id)
    return {"message_id": entry["id"], "revision": item["revision"], "queued": True}


def cancel(hub_dir, item_id: str, actor: str, *, reason: str = "", surface: str | None = None,
           expected_revision: int | None = None) -> dict:
    item = store.require_item(hub_dir, item_id, _hub(hub_dir))
    if not (_is_requester(item, actor) or _can(hub_dir, actor, "workflows_manage")):
        raise denied("Only the person who asked for this work, or a manager, can cancel it.")
    if item["state"] in store.TERMINAL:
        return _view(item, full=_may_see(hub_dir, item, actor)[1])
    rev = expected_revision if expected_revision is not None else item["revision"]
    item = store.update_item(hub_dir, item_id, expected_revision=rev, cancel_requested=1)
    running = item["state"] == "running"
    if item.get("workflow_id"):
        with engine_lib.live(hub_dir, require=running) as eng:
            if eng is not None:
                eng.cancel(item["workflow_id"])
    item = store.update_item(hub_dir, item_id, state="cancelled",
                             reason="cancelled", error=(reason or "")[:500] or None)
    _audit(hub_dir, actor, "work_item_cancel", item_id, surface=surface)
    return _view(item, full=_may_see(hub_dir, item, actor)[1])


def _continue(hub_dir, item: dict, start_step: int | None, *, new_version: bool = False) -> dict:
    """Continue an item from a saved step in a new execution of the same
    workflow, on the live engine. Steps before `start_step` are reused.

    An ordinary continuation stays on the code version the work started on.
    If the hub now runs different code, the work stops for an explicit restart
    instead of moving silently (`new_version` is that restart). The next
    execution is recorded first, under a fixed id, then created; if Hubzoid
    stops in between, continuing again finds and adopts it."""
    if not item.get("workflow_id") or item["execution"] < 1:
        raise WorkItemError("not_waiting", "This work has not run yet.", status=409)
    if start_step is None:
        raise WorkItemError("not_waiting", "There is no saved step to continue from.", status=409)
    previous = engine_lib.execution_id(item["id"], item["execution"])
    nxt = item["execution"] + 1
    wid = engine_lib.execution_id(item["id"], nxt)
    with engine_lib.live(hub_dir) as eng:
        if not new_version and eng.version and item.get("app_version") and eng.version != item["app_version"]:
            store.update_item(hub_dir, item["id"], expected_revision=item["revision"],
                              state="needs_input", reason="definition_changed",
                              error=f"The workflow code changed from {item['app_version']} to {eng.version} "
                                    "while this work waited. Restart it once you have checked the change.")
            raise WorkItemError("definition_changed",
                                "The workflow code changed while this work waited. Restart it from a "
                                "saved stage once you have checked the change.", status=409)
        version = eng.version if new_version else (item.get("app_version") or eng.version)
        item = store.update_item(hub_dir, item["id"], expected_revision=item["revision"],
                                 expect_state=("waiting", "needs_input"), state="accepted",
                                 execution=nxt, workflow_id=wid, reason=None, error=None,
                                 resume_step=start_step,
                                 app_version=version if new_version else item.get("app_version"))
        _create_execution(eng, previous, start_step, wid, item, version, Path(hub_dir).name)
    return store.require_item(hub_dir, item["id"])


def _create_execution(eng, previous: str, start_step: int, wid: str, item: dict, version: str | None,
                      hub_name: str) -> None:
    if eng.status(wid) is not None:
        return                                  # created before a crash: adopt it
    # The engine's queue is named after the hub folder as written; item["hub"]
    # is the lowercase access key (HUB-140 F5).
    kw = {"queue_name": engine_lib.queue_name(hub_name), "queue_partition_key": item["id"]}
    if version:
        kw["application_version"] = version
    eng.fork(previous, start_step, wid, **kw)


def reconcile_stranded(hub_dir) -> int:
    """Executions recorded on an item but never created (Hubzoid stopped
    between the two) are created now, from the same saved step. Returns how
    many. Called by the engine at start and by resume."""
    count = 0
    for item in store.list_items(hub_dir, _hub(hub_dir), state="accepted", limit=100):
        if item["execution"] < 1 or not item.get("workflow_id"):
            continue
        with engine_lib.live(hub_dir) as eng:
            if eng.status(item["workflow_id"]) is not None:
                continue
            if item["execution"] == 1:
                start(hub_dir, item)
            else:
                _create_execution(eng, engine_lib.execution_id(item["id"], item["execution"] - 1),
                                  item.get("resume_step"), item["workflow_id"], item, item.get("app_version"),
                                  Path(hub_dir).name)
        count += 1
    return count


def approve(hub_dir, op_id: str, actor: str, *, digest: str, expected_revision: int | None = None,
            surface: str | None = None) -> dict:
    """A person approves the exact content of a waiting operation, then the
    work continues and sends it once."""
    row = store.get_op(hub_dir, op_id)
    if row is None or row["hub"] != _hub(hub_dir):
        raise not_found("operation")
    if not _is_requester(row, actor):
        raise denied("Only the person who asked for this work can approve it.", "approver_not_allowed")
    if row["state"] != "awaiting_approval":
        raise WorkItemError("not_waiting", f"This operation is {row['state']}, not waiting for approval.",
                            status=409)
    if digest != row["args_digest"]:
        raise WorkItemError("approval_stale",
                            "The content changed since you looked at it. Review the current version "
                            "and approve that.", status=409, current_digest=row["args_digest"])
    rev = expected_revision if expected_revision is not None else row["revision"]
    row = store.update_op(hub_dir, op_id, expected_revision=rev,
                          approval_json={"digest": digest, "approver": _norm(actor), "at": store.now(),
                                         "target_version": row.get("target_version")})
    _audit(hub_dir, actor, "operation_approve", op_id, surface=surface)
    item = _resume_after_decision(hub_dir, row)
    return {"operation": ops_lib.view(row, full=True), "work_item": _view(item, full=True) if item else None}


def deny(hub_dir, op_id: str, actor: str, *, reason: str = "", expected_revision: int | None = None,
         surface: str | None = None) -> dict:
    row = store.get_op(hub_dir, op_id)
    if row is None or row["hub"] != _hub(hub_dir):
        raise not_found("operation")
    if not _is_requester(row, actor):
        raise denied("Only the person who asked for this work can decide on it.", "approver_not_allowed")
    if row["state"] != "awaiting_approval":
        raise WorkItemError("not_waiting", f"This operation is {row['state']}, not waiting for approval.",
                            status=409)
    rev = expected_revision if expected_revision is not None else row["revision"]
    row = store.update_op(hub_dir, op_id, expected_revision=rev, state="abandoned",
                          resolution_json={"decision": "deny", "by": _norm(actor), "at": store.now(),
                                           "reason": (reason or "")[:500]})
    _audit(hub_dir, actor, "operation_deny", op_id, surface=surface)
    item = _stop_item(hub_dir, row, "approval_denied", "The person who asked for this work denied the action.")
    return {"operation": ops_lib.view(row, full=True), "work_item": _view(item, full=True) if item else None}


def resolve(hub_dir, op_id: str, actor: str, *, decision: str, evidence=None,
            acknowledge_duplicate_risk: bool = False, expected_revision: int | None = None,
            surface: str | None = None) -> dict:
    """The requester decides an ambiguous operation: done (with evidence),
    retry (accepting the risk of a duplicate) or abandon."""
    row = store.get_op(hub_dir, op_id)
    if row is None or row["hub"] != _hub(hub_dir):
        raise not_found("operation")
    if not _is_requester(row, actor):
        raise denied("Only the person who asked for this work can resolve it.", "resolver_not_allowed")
    if row["state"] != "ambiguous":
        raise WorkItemError("not_ambiguous", f"This operation is {row['state']}; only an uncertain one "
                            "needs a decision.", status=409)
    if decision not in ("done", "retry", "abandon"):
        raise WorkItemError("validation_failed", "decision is done, retry or abandon.", status=422)
    if decision == "done" and not evidence:
        raise WorkItemError("validation_failed", "Done needs evidence: a link, an id or a note saying "
                            "where you confirmed it happened.", status=422)
    if decision == "retry" and not acknowledge_duplicate_risk:
        raise WorkItemError("validation_failed", "Retry can send it twice. Acknowledge the duplicate risk "
                            "to retry.", status=422)
    rev = expected_revision if expected_revision is not None else row["revision"]
    resolution = {"decision": decision, "by": _norm(actor), "at": store.now(),
                  "evidence": redaction.redact(evidence) if evidence else None,
                  "attested": decision == "done"}
    if decision == "done":
        row = store.update_op(hub_dir, op_id, expected_revision=rev, expect_state=("ambiguous",),
                              state="resolved_done", resolution_json=resolution,
                              result_json={"resolved_by_person": True,
                                           "evidence": resolution["evidence"]})
    elif decision == "retry":
        row = store.update_op(hub_dir, op_id, expected_revision=rev, expect_state=("ambiguous",),
                              state="pending", resolution_json=resolution)
    else:
        row = store.update_op(hub_dir, op_id, expected_revision=rev, expect_state=("ambiguous",),
                              state="abandoned", resolution_json=resolution)
    _audit(hub_dir, actor, f"operation_resolve_{decision}", op_id, surface=surface)
    if decision == "abandon":
        item = _stop_item(hub_dir, row, "operation_abandoned", "The person who asked for this work abandoned "
                          "an operation whose outcome was unknown.")
    else:
        item = _resume_after_decision(hub_dir, row)
    return {"operation": ops_lib.view(row, full=True), "work_item": _view(item, full=True) if item else None}


def _stop_item(hub_dir, row: dict, reason: str, message: str) -> dict | None:
    if not row.get("item_id"):
        return None
    item = store.get_item(hub_dir, row["item_id"])
    if item is None or item["state"] in store.TERMINAL:
        return item
    return store.update_item(hub_dir, item["id"], state="stopped", reason=reason, error=message)


def _resume_after_decision(hub_dir, row: dict) -> dict | None:
    """Continue the work item whose operation was just decided, once nothing
    else blocks it. An operation outside a work item never parked a run: the
    decision is recorded and the next run finds it."""
    if not row.get("item_id"):
        return None
    item = store.get_item(hub_dir, row["item_id"])
    if item is None or item["state"] not in ("waiting", "needs_input"):
        return item
    if item.get("reason") == "definition_changed" or _blocking(hub_dir, item):
        return item
    try:
        return _continue(hub_dir, item, row.get("resume_step") if row.get("resume_step") is not None
                         else item.get("resume_step"))
    except WorkItemError as exc:
        if exc.code == "definition_changed":
            return store.require_item(hub_dir, item["id"])
        raise


def _blocking(hub_dir, item: dict) -> list[dict]:
    return [o for o in store.list_ops(hub_dir, item["hub"], item_id=item["id"])
            if store.unresolved(o)
            or (o["state"] == "awaiting_approval"
                and (o.get("approval_json") or {}).get("digest") != o["args_digest"])]


def resume(hub_dir, item_id: str, actor: str, *, expected_revision: int | None = None,
           surface: str | None = None) -> dict:
    """Continue waiting work whose decisions are all made."""
    item = store.require_item(hub_dir, item_id, _hub(hub_dir))
    if not (_is_requester(item, actor) or _can(hub_dir, actor, "workflows_manage")):
        raise denied("Only the person who asked for this work, or a manager, can resume it.")
    if item["state"] == "accepted" and item["execution"] >= 1:
        reconcile_stranded(hub_dir)             # recorded but never created: create it now
        return _view(store.require_item(hub_dir, item_id), full=_may_see(hub_dir, item, actor)[1])
    if item["state"] not in ("waiting", "needs_input") or item.get("reason") == "definition_changed":
        raise WorkItemError("not_waiting", "Only work waiting on a settled decision can resume. Work "
                            "stopped by a code change needs a restart from a stage.", status=409)
    unresolved = _blocking(hub_dir, item)
    if unresolved:
        raise WorkItemError("operation_unresolved", "An operation still needs a decision first.",
                            status=409, operation_id=unresolved[0]["id"])
    if expected_revision is not None and expected_revision != item["revision"]:
        from .errors import revision_conflict
        raise revision_conflict(item["revision"])
    item = _continue(hub_dir, item, item.get("resume_step"))
    _audit(hub_dir, actor, "work_item_resume", item_id, surface=surface)
    return _view(item, full=_may_see(hub_dir, item, actor)[1])


def restart(hub_dir, item_id: str, actor: str, *, stage: str | None = None, reason: str,
            expected_revision: int | None = None, surface: str | None = None) -> dict:
    """Restart stopped work on the current code. With `stage`, that saved
    stage and everything after it run again. Without it, the work continues
    after its last saved step. Earlier stages are reused. Everything uncertain
    must be decided first."""
    item = store.require_item(hub_dir, item_id, _hub(hub_dir))
    if not (_is_requester(item, actor) or _can(hub_dir, actor, "workflows_manage")):
        raise denied("Only the person who asked for this work, or a manager, can restart it.")
    if item["state"] not in ("needs_input", "failed", "stopped"):
        raise WorkItemError("not_restartable", "Only stopped, failed or blocked work can be restarted.",
                            status=409)
    if not reason or not reason.strip():
        raise WorkItemError("validation_failed", "Say why you are restarting it.", status=422)
    unresolved = [o for o in store.list_ops(hub_dir, item["hub"], item_id=item_id) if store.unresolved(o)]
    if unresolved:
        raise WorkItemError("operation_unresolved",
                            "An operation's outcome is still unknown. Resolve it before restarting.",
                            status=409, operation_id=unresolved[0]["id"])
    if expected_revision is not None and expected_revision != item["revision"]:
        from .errors import revision_conflict
        raise revision_conflict(item["revision"])
    with engine_lib.live(hub_dir) as eng:
        stages = engine_lib.stage_steps(eng, item["workflow_id"])
        last = engine_lib.last_step(eng, item["workflow_id"])
    if stage is None:
        start_step = (last + 1) if last is not None else None
    else:
        match = next((s for s in stages if s["stage"] == stage), None)
        if match is None:
            raise WorkItemError(
                "stage_missing",
                f"Stage {stage!r} has no saved checkpoint in this work. Saved stages: "
                f"{', '.join(s['stage'] for s in stages) or 'none'}. Leave the stage out to continue "
                "after the last saved one.", status=422)
        start_step = match["step"]
    # Re-run that stage and everything after it on the current code.
    if item["state"] in ("failed", "stopped"):
        item = store.update_item(hub_dir, item_id, expected_revision=item["revision"],
                                 state="needs_input", reason="restart_requested")
    item = _continue(hub_dir, item, start_step, new_version=True)
    _audit(hub_dir, actor, "work_item_restart", item_id, surface=surface)
    return _view(item, full=_may_see(hub_dir, item, actor)[1])
