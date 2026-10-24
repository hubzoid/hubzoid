"""Reach the hub's live workflow engine, in this process or through a client.

The CLI and a Console served by another bridge never start a second engine.
They hand work to the live one through DBOS's client, as `workflows.client`
does for Run now. Inside the engine process the DBOS class is used directly.
"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

from .errors import WorkItemError

STAGE_PREFIX = "hz_stage:"


def queue_name(hub: str) -> str:
    """The work-item queue of the hub whose folder is named ``hub``, exactly as
    written: the engine registers it under that name."""
    from ..workflows.runtime import _app_name

    return _app_name(hub) + "-wi"


def execution_id(item_id: str, execution: int) -> str:
    return f"wi-{item_id}-e{execution}"


def _here(hub_dir) -> bool:
    from ..workflows import runtime

    return bool(runtime._INITED and runtime._HUB_DIR is not None
                and Path(runtime._HUB_DIR).resolve() == Path(hub_dir).resolve())


class _Local:
    """The DBOS class methods, inside the engine process."""

    def __init__(self):
        from dbos import DBOS

        from ..workflows import runtime
        self.dbos = DBOS
        self.version = runtime._APP_VERSION

    def enqueue(self, options, *args):
        return self.dbos.enqueue_workflow_with_options(options, *args).get_workflow_id()

    def fork(self, workflow_id, start_step, new_id, **kw):
        from dbos import SetWorkflowID

        with SetWorkflowID(new_id):
            return self.dbos.fork_workflow(workflow_id, start_step, **kw).get_workflow_id()

    def cancel(self, workflow_id):
        self.dbos.cancel_workflow(workflow_id)

    def status(self, workflow_id):
        return self.dbos.get_workflow_status(workflow_id)

    def steps(self, workflow_id):
        return self.dbos.list_workflow_steps(workflow_id)


class _Remote:
    """DBOS's client, from the CLI or another bridge."""

    def __init__(self, client, version):
        self.client = client
        self.version = version

    def enqueue(self, options, *args):
        return self.client.enqueue(options, *args).get_workflow_id()

    def fork(self, workflow_id, start_step, new_id, **kw):
        from dbos import SetWorkflowID

        with SetWorkflowID(new_id):
            return self.client.fork_workflow(workflow_id, start_step, **kw).get_workflow_id()

    def cancel(self, workflow_id):
        self.client.cancel_workflow(workflow_id)

    def status(self, workflow_id):
        try:
            return self.client.retrieve_workflow(workflow_id).get_status()
        except Exception:  # noqa: BLE001 - an unknown id reads as no status
            return None

    def steps(self, workflow_id):
        return self.client.list_workflow_steps(workflow_id)


@contextmanager
def live(hub_dir, *, require: bool = True):
    """The engine for this hub. With `require`, refuse clearly when no engine
    is running instead of queueing work nobody will pick up."""
    hub_dir = Path(hub_dir)
    if _here(hub_dir):
        yield _Local()
        return
    from ..workflows.ownership import readiness

    ready = readiness(hub_dir, hub_dir.name)
    if not ready:
        if require:
            raise WorkItemError(
                "unavailable",
                "The workflow engine for this hub isn't running. Start it with "
                "`hubzoid run` (or the gateway), then try again. Nothing was changed.",
                status=503, retryable=True)
        yield None
        return
    from dbos import DBOSClient

    from .. import db
    from ..workflows.runtime import _app_name

    client = DBOSClient(system_database_url=db.dbos_url(hub_dir),
                        application_name=_app_name(hub_dir.name), retry_connection_errors=False)
    try:
        yield _Remote(client, ready.get("version"))
    finally:
        client.destroy()


def stage_steps(eng, workflow_id: str) -> list[dict]:
    """The completed and failed stages of one execution, in order."""
    out = []
    for step in eng.steps(workflow_id) or []:
        get = step.get if isinstance(step, dict) else (lambda k, s=step: getattr(s, k, None))
        name = get("function_name") or ""
        if not name.startswith(STAGE_PREFIX):
            continue
        done_ms = get("completed_at_epoch_ms")
        out.append({"stage": name[len(STAGE_PREFIX):], "step": get("function_id"),
                    "status": "failed" if get("error") is not None else "done",
                    "finished": done_ms / 1000 if done_ms else None})
    return out


def last_step(eng, workflow_id: str) -> int | None:
    """The id of the last step this execution saved, or None."""
    ids = []
    for step in eng.steps(workflow_id) or []:
        fid = step.get("function_id") if isinstance(step, dict) else getattr(step, "function_id", None)
        if fid is not None:
            ids.append(fid)
    return max(ids) if ids else None
