"""Operator commands hand work to the live engine, never start a second one."""
from __future__ import annotations
import time
import uuid
from .ownership import readiness
from .. import db


def enqueue_live(hub_dir, name, *, markdown=False, overrides=None):
    from dbos import DBOSClient
    from .runtime import _app_name
    ready = readiness(hub_dir, hub_dir.name)
    if not ready:
        return None
    client = DBOSClient(system_database_url=db.dbos_url(hub_dir),
                        application_name=_app_name(hub_dir.name), retry_connection_errors=False)
    try:
        rid = ('md:' + name + ':manual-' if markdown else 'manual:') + uuid.uuid4().hex + '@' + _app_name(hub_dir.name)
        if markdown:
            from .markdown import TASK_WORKFLOW
            options = {'workflow_name': TASK_WORKFLOW, 'queue_name': _app_name(hub_dir.name)+'-md', 'workflow_id': rid}
            # Matches markdown.enqueue_task's persisted argument contract.
            args = (name, str(hub_dir), hub_dir.name, overrides)
        else:
            definition = ready.get('definitions', {}).get(name)
            if definition is None:
                raise ValueError(f'Workflow {name!r} is not served by the live owner')
            options = {'workflow_name': name, 'queue_name': definition['queue'], 'workflow_id': rid,
                       'queue_partition_key': definition.get('manual_partition', name)}
            args = (hub_dir.name,)
        handle = client.enqueue(options, *args)
        return LiveHandle(client, handle, hub_dir)
    except BaseException:
        client.destroy()
        raise


class LiveHandle:
    def __init__(self, client, handle, hub_dir):
        self.client, self.handle, self.hub_dir = client, handle, hub_dir
    def get_workflow_id(self):
        return self.handle.get_workflow_id()
    def get_result(self):
        try:
            while True:
                status = self.client.get_workflow_status(self.get_workflow_id())
                if status and status.status in ('SUCCESS', 'ERROR', 'CANCELLED', 'MAX_RECOVERY_ATTEMPTS_EXCEEDED'):
                    return self.handle.get_result()
                if not readiness(self.hub_dir, self.hub_dir.name):
                    raise RuntimeError('Workflow owner stopped; the queued run remains durable. Inspect schedule status.')
                time.sleep(.25)
        finally:
            self.client.destroy()
