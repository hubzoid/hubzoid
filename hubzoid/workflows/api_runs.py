"""Start a "Run for me" workflow over the API: ``POST /portal/api/my/runs``.

The same run the chat tool starts (``tools.report_tools.start_personal``):
as the caller, with their own access, the workflow's surfaces (``api``), input
validation, the backup hold and a personal run id, so the run shows in the
person's history (``GET /portal/api/my/runs``, ``my_reports`` in chat).

* Confirmation: a workflow declared with ``confirm=True`` needs
  ``"confirm": true`` in the body, the API's explicit form of the chat yes.
* Idempotency: an ``Idempotency-Key`` makes the run id deterministic, so the
  engine admits it once even under concurrent retries. The same key returns
  the same run while the engine keeps it. The same key with different inputs
  is refused (409). Without a key, an active run with the same inputs is
  returned, as in chat.

How others do it: Agno ``POST /workflows/{id}/runs`` and Mastra
``POST /api/workflows/:id/start-async`` start a run as the caller.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

_KEY = re.compile(r"^[\x21-\x7e]{1,128}$")


class RunRefused(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def runnable(hub_dir: Path, name: str):
    """The ``as_tool`` workflow ``name`` of this hub, or RunRefused 404."""
    from ..capabilities import switched_off
    from ..tools.report_tools import RESERVED_NAMES, SCHEDULE_REPORTS
    from . import runtime
    from .boot import schedules_enabled
    from .builtin import NAMES as BUILTIN

    if switched_off(SCHEDULE_REPORTS) or not schedules_enabled():
        raise RunRefused(409, "reports_off", "Reports can't run on this agent right now.")
    for wf in runtime.load_definitions(Path(hub_dir)):
        if (wf.name == name and wf.as_tool and wf.name not in BUILTIN
                and wf.name.lower() not in RESERVED_NAMES):
            return wf
    raise RunRefused(404, "not_found", f"No report {name!r} you can run on this agent.")


def _existing(hub_dir: Path, run_id: str) -> dict | None:
    """The envelope of a run with this id, or None."""
    from dbos import DBOSClient

    from .. import db
    from .runtime import _app_name

    client = DBOSClient(system_database_url=db.dbos_url(hub_dir), application_name=_app_name(Path(hub_dir).name),
                        retry_connection_errors=False)
    try:
        found = client.list_workflows(workflow_ids=[run_id], limit=1, load_input=True, load_output=False)
        for w in found:
            raw = w.input if isinstance(w.input, dict) else {}
            args = raw.get("args") or ()
            env = args[2] if len(args) == 3 and isinstance(args[2], dict) else {}
            return {"status": w.status, "envelope": env}
        return None
    finally:
        client.destroy()


def start(hub_dir: Path, name: str, *, email: str, account: str, inputs: dict | None, confirm: bool,
          idempotency_key: str | None) -> dict:
    """Start the run as this person. Returns ``{run_id, status, created}``."""
    from ..tools.report_tools import _account, _inputs_ref, start_personal
    from . import access as wf_access
    from . import inputs as inputs_lib
    from . import personal, runtime

    hub_dir = Path(hub_dir)
    wf = runnable(hub_dir, name)
    if wf.surfaces and "api" not in wf.surfaces:
        raise RunRefused(403, "surface_not_allowed", f"{wf.title} can't be started from the API.")
    if not wf_access.authorize(hub_dir, email, wf.name, "run").allowed:
        raise RunRefused(403, "permission_denied", f"You can't run {wf.title}. Ask an administrator for access.")
    if wf.confirm and confirm is not True:
        raise RunRefused(409, "confirm_required",
                         f"{wf.title} asks for a yes first. Send the request again with \"confirm\": true.")
    try:
        data = inputs_lib.dump(inputs_lib.validate(wf.inputs, inputs or None, max_bytes=wf.max_input_bytes))
    except inputs_lib.InputError as exc:
        raise RunRefused(422, "validation_failed", str(exc))
    shown = inputs_lib.display(wf.inputs, data)
    if _account(hub_dir, email) != account:
        raise RunRefused(403, "permission_denied", "This account can't run reports here.")
    from .control import _engine_here

    here = _engine_here(hub_dir) and wf.name in runtime._REGISTRY
    workflow_id = None
    if idempotency_key is not None:
        if not _KEY.match(idempotency_key):
            raise RunRefused(422, "validation_failed", "Idempotency-Key is 1 to 128 printable characters.")
        prefix = personal.tool_run_prefix(wf.name, email, account)
        workflow_id = prefix + "k" + hashlib.sha256(idempotency_key.encode()).hexdigest()[:16]
        seen = _existing(hub_dir, workflow_id)
        if seen is not None:
            env = seen["envelope"]
            if (env.get("acts_as") != email or env.get("acts_as_account") != account
                    or json.dumps(env.get("inputs"), sort_keys=True) != json.dumps(data, sort_keys=True)):
                raise RunRefused(409, "idempotency_conflict",
                                 "This Idempotency-Key was used for a different run. Use a new key.")
            return {"run_id": workflow_id, "status": seen["status"], "created": False}
    if here:
        run_id, already, refusal = start_personal(hub_dir, wf, email, data, shown, trigger="api", via="api",
                                                  workflow_id=workflow_id)
        if refusal:
            raise RunRefused(409, "on_hold", refusal)
        return {"run_id": run_id, "status": "PENDING" if already else "ENQUEUED", "created": not already}
    # The engine runs in another process (a gateway): the same hold, the same
    # run id rules, queued through its system database.
    import secrets
    import time

    from ..access import store_for
    from . import client

    if store_for(hub_dir).schedule_hold():
        raise RunRefused(409, "on_hold", "New runs are on hold while a backup runs. Try again in a few minutes.")
    run_id = workflow_id or (f"{personal.tool_run_prefix(wf.name, email, account)}{_inputs_ref(data)}-"
                             + secrets.token_hex(4))
    env = {"trigger": "api", "via": "api", "scope": "personal", "acts_as": email, "acts_as_account": account,
           "started_by": email, "inputs": data, "inputs_display": shown, "started_at": time.time()}
    if client.enqueue_personal(hub_dir, wf.name, envelope=env, workflow_id=run_id) is None:
        raise RunRefused(409, "engine_not_running", "Reports can't run on this agent right now. Try again later.")
    return {"run_id": run_id, "status": "ENQUEUED", "created": True}
