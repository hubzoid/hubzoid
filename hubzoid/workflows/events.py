"""Durable webhook admission and DBOS retry coordination.

The operational row is an admission outbox, not another workflow engine. DBOS
owns execution and checkpoints; deterministic IDs bridge the two commits.
"""
from __future__ import annotations
import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path
from sqlalchemy import text
from .. import db, migrations

log = logging.getLogger(__name__)
COORDINATOR = 'hz_webhook_event'
TERMINAL = ('succeeded', 'failed')


def settings(hub_dir):
    import yaml
    from .._fs import resolve_bucket
    root = resolve_bucket(Path(hub_dir), 'workflows')
    path = root / 'settings.yaml' if root else None
    data = yaml.safe_load(path.read_text()) if path and path.exists() else {}
    if data is not None and not isinstance(data, dict):
        raise ValueError('Workflow settings must be a mapping')
    return data or {}


def declarations(hub_dir):
    import re
    specs = settings(hub_dir).get('webhooks', {})
    if not isinstance(specs, dict):
        raise ValueError('webhooks must be a mapping')
    seen = set()
    for name, spec in specs.items():
        if not isinstance(name, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', name):
            raise ValueError('Webhook names must be lowercase URL-safe identifiers')
        key = name.upper().replace('-', '_')
        if key in seen:
            raise ValueError('Webhook names collide after environment normalization')
        seen.add(key)
        if not isinstance(spec, dict) or spec.get('verify', 'header') not in ('header', 'hmac'):
            raise ValueError('Webhook verification must be header or hmac')
        if spec.get('verify') == 'hmac' and not spec.get('timestamp_header'):
            raise ValueError('HMAC webhooks require a signed timestamp_header')
    return specs


def event_id(hub, webhook, key):
    encoded = json.dumps([hub, webhook, key], ensure_ascii=False, separators=(',', ':'))
    return 'wh:' + hashlib.sha256(encoded.encode()).hexdigest()


def _engine(hub_dir):
    engine = db.operational_engine(hub_dir)
    migrations.upgrade(engine, 'operational')
    return engine


def get(hub_dir, eid):
    with _engine(hub_dir).connect() as conn:
        row = conn.execute(text('SELECT * FROM hz_workflow_events WHERE id=:i'), {'i': eid}).mappings().first()
    return dict(row) if row else None


def admit(hub_dir, hub, webhook, workflow, key, payload, *, max_pending=10000):
    encoded = json.dumps(payload['body'], sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    eid = event_id(hub, webhook, key)
    engine = _engine(hub_dir)
    with engine.connect() as conn:
        if engine.dialect.name == 'sqlite':
            conn.exec_driver_sql('BEGIN IMMEDIATE')
        else:
            conn.execute(text('SELECT pg_advisory_xact_lock(:k)'), {'k': int.from_bytes(hashlib.sha256(('admit:'+hub).encode()).digest()[:8], 'big', signed=True)})
        existing = conn.execute(text('SELECT digest FROM hz_workflow_events WHERE id=:i'), {'i': eid}).first()
        if existing:
            conn.commit()
            if existing.digest != digest:
                from .alerts import record
                record(hub_dir, hub, 'content_mismatch', eid + ':' + digest, {'workflow': workflow, 'run_id': eid})
                raise ValueError('Event key was already accepted with different content')
            return eid
        pending = conn.execute(text("SELECT count(*) FROM hz_workflow_events WHERE hub=:h AND state NOT IN ('succeeded','failed')"), {'h': hub}).scalar_one()
        if pending >= max_pending:
            raise RuntimeError('Webhook backlog is full; retry later')
        now = time.time()
        conn.execute(text("INSERT INTO hz_workflow_events (id,hub,webhook,workflow,digest,payload,state,created,updated,attempt,redrive) VALUES (:i,:h,:w,:f,:d,:p,'accepted',:n,:n,0,0)"), {'i': eid, 'h': hub, 'w': webhook, 'f': workflow, 'd': digest, 'p': json.dumps({**payload, 'key': key, 'webhook': webhook, 'received_at': now}), 'n': now})
        conn.commit()
    return eid


def update(hub_dir, eid, state, *, attempt=None, version=None, error=None):
    from . import runtime
    if runtime._OWNER is not None:
        runtime._OWNER.assert_owned()
    with _engine(hub_dir).begin() as conn:
        conn.execute(text('UPDATE hz_workflow_events SET state=:s,updated=:n,attempt=COALESCE(:a,attempt),version=COALESCE(:v,version),error=:e WHERE id=:i'), {'s': state, 'n': time.time(), 'a': attempt, 'v': version, 'e': error, 'i': eid})


def register(DBOS, hub_dir, hub):
    @DBOS.workflow(name=COORDINATOR)
    async def coordinator(eid, redrive=0):
        from . import runtime
        row = await asyncio.to_thread(get, hub_dir, eid)
        if not row or row['state'] in TERMINAL:
            return
        wf = runtime._REGISTRY.get(row['workflow'])
        if wf is None or wf.on_webhook != row['webhook']:
            await asyncio.to_thread(update, hub_dir, eid, 'failed', error='Workflow mapping changed before execution')
            raise RuntimeError('Webhook workflow mapping is unavailable')
        event = json.loads(row['payload'])
        delays = settings(hub_dir).get('webhook_retry_delays', [60, 300])
        if not isinstance(delays, list) or len(delays) > 9 or any(not isinstance(x, (int,float)) or x < 0 for x in delays):
            raise ValueError('webhook_retry_delays must contain at most 9 nonnegative seconds')
        for attempt in range(1, len(delays)+2):
            await asyncio.to_thread(runtime._OWNER.assert_owned)
            await asyncio.to_thread(update, hub_dir, eid, 'running', attempt=attempt, version=runtime._APP_VERSION)
            options = runtime.enqueue_options(wf, event)
            options.update(workflow_name=wf.name, workflow_id=f'{eid}:r{redrive}:a{attempt}')
            handle = await DBOS.enqueue_workflow_with_options_async(options, hub, {**event, 'attempt': attempt, 'id': eid})
            try:
                await handle.get_result()
            except Exception as exc:
                if attempt == len(delays)+1:
                    await asyncio.to_thread(update, hub_dir, eid, 'failed', error=type(exc).__name__)
                    raise RuntimeError('Webhook event exhausted its attempts') from exc
                await asyncio.to_thread(update, hub_dir, eid, 'retrying', error=type(exc).__name__)
                await DBOS.sleep_async(float(delays[attempt-1]))
            else:
                await asyncio.to_thread(update, hub_dir, eid, 'succeeded')
                return


def reconcile(hub_dir, hub):
    from . import runtime
    runtime._OWNER.assert_owned()
    with _engine(hub_dir).connect() as conn:
        rows = conn.execute(text("SELECT id,state,version,redrive FROM hz_workflow_events WHERE hub=:h AND state NOT IN ('succeeded','failed') ORDER BY created LIMIT 100"), {'h': hub}).mappings().all()
    from dbos import DBOS
    for row in rows:
        if row['version'] and row['version'] != runtime._APP_VERSION:
            update(hub_dir, row['id'], 'failed', error='Code changed during this event; inspect side effects before redrive')
            continue
        DBOS.enqueue_workflow_with_options({'workflow_name': COORDINATOR, 'queue_name': runtime._app_name(hub)+'-events', 'workflow_id': f"{row['id']}:r{row['redrive']}"}, row['id'], row['redrive'])


def redrive(hub_dir, hub, eid):
    """Explicit operator retry; keeps external idempotency key, new attempt IDs."""
    with _engine(hub_dir).begin() as conn:
        changed = conn.execute(text("UPDATE hz_workflow_events SET state='accepted',redrive=redrive+1,attempt=0,version=NULL,error=NULL,updated=:n WHERE id=:i AND hub=:h AND state='failed'"), {'i': eid, 'h': hub, 'n': time.time()}).rowcount
    if not changed:
        raise ValueError('Only a failed event in this hub can be redriven')
