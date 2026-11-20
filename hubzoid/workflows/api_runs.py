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


def _client(hub_dir: Path):
    from dbos import DBOSClient

    from .. import db
    from .runtime import _app_name

    app = _app_name(Path(hub_dir).name)
    return app, DBOSClient(system_database_url=db.dbos_url(hub_dir), application_name=app,
                           retry_connection_errors=False)


def _envelope(w) -> dict:
    raw = w.input if isinstance(w.input, dict) else {}
    args = raw.get("args") or ()
    return args[2] if len(args) == 3 and isinstance(args[2], dict) else {}


def _existing(hub_dir: Path, run_id: str) -> dict | None:
    """The status and envelope of this hub's run with this id, or None."""
    app, client = _client(hub_dir)
    try:
        found = client.list_workflows(application_name=app, workflow_ids=[run_id], limit=1,
                                      load_input=True, load_output=False)
        for w in found:
            return {"status": w.status, "envelope": _envelope(w)}
        return None
    finally:
        client.destroy()


def _active(hub_dir: Path, name: str, prefix: str) -> str | None:
    """An active run of this workflow under this run id prefix, else None."""
    from ..tools.report_tools import _ACTIVE

    app, client = _client(hub_dir)
    try:
        found = client.list_workflows(application_name=app, name=name, workflow_id_prefix=prefix,
                                      status=list(_ACTIVE), limit=1, load_input=False, load_output=False)
        return found[0].workflow_id if found else None
    finally:
        client.destroy()


def _same_run(env: dict, email: str, account: str, data) -> bool:
    return (env.get("acts_as") == email and env.get("acts_as_account") == account
            and json.dumps(env.get("inputs"), sort_keys=True) == json.dumps(data, sort_keys=True))


def start(hub_dir: Path, name: str, *, email: str, account: str, inputs: dict | None, confirm: bool,
          idempotency_key: str | None, credential: tuple[str, str] | None = None) -> dict:
    """Start the run as this person. Returns ``{run_id, status, created}``.
    ``credential`` (``("api_key", id)`` or ``("app_jwt", "app:<id>")``) goes
    with the run, so revoking it stops the run (``workflows.context``)."""
    import secrets
    import time

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
    prefix = personal.tool_run_prefix(wf.name, email, account)
    # Who admitted this request: compared with the stored run afterwards, so of
    # two concurrent requests with one key only the first is "created".
    nonce = secrets.token_hex(8)
    extra = {"admission": nonce}
    if credential and credential[0] in ("api_key", "app_jwt") and credential[1]:
        extra["credential"] = {"kind": credential[0], "id": credential[1]}
    workflow_id = None
    if idempotency_key is not None:
        if not _KEY.match(idempotency_key):
            raise RunRefused(422, "validation_failed", "Idempotency-Key is 1 to 128 printable characters.")
        scoped = f"{hub_dir.name.lower()}\0{idempotency_key}"
        workflow_id = prefix + "k" + hashlib.sha256(scoped.encode()).hexdigest()[:16]
        seen = _existing(hub_dir, workflow_id)
        if seen is not None:
            if not _same_run(seen["envelope"], email, account, data):
                raise RunRefused(409, "idempotency_conflict",
                                 "This Idempotency-Key was used for a different run. Use a new key.")
            return {"run_id": workflow_id, "status": seen["status"], "created": False}
    if here:
        run_id, already, refusal = start_personal(hub_dir, wf, email, data, shown, trigger="api", via="api",
                                                  workflow_id=workflow_id, extra=extra)
        if refusal:
            raise RunRefused(409, "on_hold", refusal)
        if already:
            return {"run_id": run_id, "status": "PENDING", "created": False}
    else:
        # The engine runs in another process (a gateway): the same hold and
        # run id rules, queued through its system database.
        from ..access import store_for
        from . import client

        if store_for(hub_dir).schedule_hold():
            raise RunRefused(409, "on_hold", "New runs are on hold while a backup runs. Try again in a few minutes.")
        same_inputs = f"{prefix}{_inputs_ref(data)}-"
        if workflow_id is None:
            active = _active(hub_dir, wf.name, same_inputs)
            if active:
                return {"run_id": active, "status": "PENDING", "created": False}
        run_id = workflow_id or same_inputs + secrets.token_hex(4)
        env = {"trigger": "api", "via": "api", "scope": "personal", "acts_as": email, "acts_as_account": account,
               "started_by": email, "inputs": data, "inputs_display": shown, "started_at": time.time(), **extra}
        if client.enqueue_personal(hub_dir, wf.name, envelope=env, workflow_id=run_id) is None:
            raise RunRefused(409, "engine_not_running", "Reports can't run on this agent right now. Try again later.")
    if workflow_id is not None:
        # A concurrent request with the same key may have been admitted first:
        # the stored run decides.
        stored = _existing(hub_dir, workflow_id)
        env = (stored or {}).get("envelope") or {}
        if stored is not None and env.get("admission") != nonce:
            if not _same_run(env, email, account, data):
                raise RunRefused(409, "idempotency_conflict",
                                 "This Idempotency-Key was used for a different run. Use a new key.")
            return {"run_id": workflow_id, "status": stored["status"], "created": False}
    return {"run_id": run_id, "status": "ENQUEUED", "created": True}
