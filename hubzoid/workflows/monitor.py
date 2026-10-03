"""Workflow health observed by the edge, independently of DBOS executors."""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from sqlalchemy import text
from . import events, alerts
from .state import WorkflowState

log = logging.getLogger(__name__)


def inspect(hub_dir):
    from ..access import store_for
    directory = Path(hub_dir)
    hub = directory.name
    engine = events._engine(directory)
    health = store_for(directory).runtime_health(hub)
    with engine.connect() as conn:
        row = conn.execute(text('SELECT boot,expires,ready FROM hz_workflow_owner WHERE hub=:h'), {'h': hub}).mappings().first()
    expected = health.get('enabled', False)
    healthy = not health.get('error') and (not expected or bool(row and row['expires'] > time.time() and row['ready'] != '{}'))
    state = WorkflowState(engine, hub, '__edge_monitor__')
    previous = state.get('outage')
    if not healthy and not previous:
        occurrence = str(time.time_ns())
        alerts.record(directory, hub, 'engine_stale', occurrence, {})
        state['outage'] = occurrence
    elif healthy and previous:
        alerts.record(directory, hub, 'engine_recovered', previous, {})
        state['outage'] = None
    return healthy


def _due_health_alerts(hub_dir):
    with events._engine(hub_dir).connect() as conn:
        return conn.execute(text("SELECT id FROM hz_workflow_alerts WHERE hub=:h AND kind IN ('engine_stale','engine_recovered') AND state='pending' AND due<=:n ORDER BY due LIMIT 10"), {'h': Path(hub_dir).name, 'n': time.time()}).scalars().all()


async def poll(hubs, status):
    """An edge restart resumes the same outbox; no workflow owner is started.
    Every database call runs in a thread, off the edge's event loop."""
    while True:
        for hub_dir in hubs:
            try:
                status[str(hub_dir)] = await asyncio.to_thread(inspect, hub_dir)
                await asyncio.to_thread(alerts.recover_deliveries, hub_dir)
                for aid in await asyncio.to_thread(_due_health_alerts, hub_dir):
                    await alerts.deliver(hub_dir, aid)
            except asyncio.CancelledError:
                raise
            except Exception:
                status[str(hub_dir)] = False
                log.exception('Workflow health monitor failed; will retry')
        await asyncio.sleep(15)
