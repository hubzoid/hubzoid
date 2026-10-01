"""Durable webhook admission and DBOS retry coordination.

The operational row is an admission outbox, not another workflow engine. DBOS
owns execution and checkpoints; deterministic IDs bridge the two commits.
"""
from __future__ import annotations
import asyncio
import hashlib
import json
import logging
import math
import time
from pathlib import Path
from sqlalchemy import text
from .. import db, migrations

log = logging.getLogger(__name__)
COORDINATOR = 'hz_webhook_event'
TERMINAL = ('succeeded', 'failed')
# Routes the hub's inbound server already owns under /webhooks/<hub>/.
INBOUND_SURFACES = ('whatsapp', 'telegram')


def settings(hub_dir):
    import yaml
    from .._fs import resolve_bucket
    root = resolve_bucket(Path(hub_dir), 'workflows')
    path = root / 'settings.yaml' if root else None
    data = yaml.safe_load(path.read_text()) if path and path.exists() else {}
    if data is not None and not isinstance(data, dict):
        raise ValueError('Workflow settings must be a mapping')
    data = data or {}
    from .deadlines import seconds
    seconds(data.get('workflow_timeout'))
    cap = data.get('max_executor_threads', 32)
    if isinstance(cap, bool) or not isinstance(cap, int) or cap < 1:
        raise ValueError('max_executor_threads must be a positive integer')
    cfg = data.get('alerts', {})
    if not isinstance(cfg, dict):
        raise ValueError('alerts must be a mapping')
    validate_destinations(cfg.get('to'))
    seconds(cfg.get('cooldown', '1h'))
    for key, default in (('pause_after_failures',20), ('failures_in_a_row',3)):
        value = cfg.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(key+' must be a nonnegative integer')
    delays = data.get('webhook_retry_delays', [60,300])
    if not isinstance(delays, list) or len(delays)>9 or any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v<0 for v in delays):
        raise ValueError('webhook_retry_delays must contain at most 9 nonnegative seconds')
    return data


def validate_destinations(value):
    if value is None:
        return
    if not isinstance(value, list) or any(not isinstance(x,dict) or len(x)!=1 or not set(x)<={'webhook','slack','email'} or not isinstance(next(iter(x.values())), str) or not next(iter(x.values())).strip() for x in value):
        raise ValueError('alert_to/to must be a list of webhook, slack or email destinations')


def _legacy_webhook_name(hub_dir):
    """The path segment of the legacy generic inbound webhook (the hub's
    WEBHOOK_INBOUND_NAME, default `webhook`), which its inbound server serves."""
    import os
    from dotenv import dotenv_values
    from ..inbound.webhook import _slug_segment
    path = Path(hub_dir) / '.env'
    raw = ((dotenv_values(path).get('WEBHOOK_INBOUND_NAME') if path.is_file() else None)
           or os.environ.get('WEBHOOK_INBOUND_NAME') or 'webhook')
    return _slug_segment(raw) or 'webhook'


def declarations(hub_dir):
    import re
    specs = settings(hub_dir).get('webhooks', {})
    if not isinstance(specs, dict):
        raise ValueError('webhooks must be a mapping')
    seen = set()
    taken = {*INBOUND_SURFACES, _legacy_webhook_name(hub_dir)} if specs else set()
    for name, spec in specs.items():
        if not isinstance(name, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', name):
            raise ValueError('Webhook names must be lowercase URL-safe identifiers')
        if name in taken:
            # The more specific workflow route would silently take it over.
            raise ValueError(f'Webhook name {name!r} is already a chat or legacy inbound '
                             'route of this hub; choose another name')
        key = name.upper().replace('-', '_')
        if key in seen:
            raise ValueError('Webhook names collide after environment normalization')
        seen.add(key)
        if not isinstance(spec, dict) or spec.get('verify', 'header') not in ('header', 'hmac'):
            raise ValueError('Webhook verification must be header or hmac')
        if spec.get('verify') == 'hmac' and not spec.get('timestamp_header'):
            raise ValueError('HMAC webhooks require a signed timestamp_header')
    return specs


def route_declarations(hub_dir):
    """Best-effort edge routing; boot records configuration errors in health."""
    try:
        return declarations(hub_dir)
    except Exception:
        log.exception('Invalid workflow settings; webhook routes disabled for %s', hub_dir)
        return {}


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
        # States are listed (not NOT IN) so the (hub, state) index is used.
        pending = conn.execute(text("SELECT count(*) FROM hz_workflow_events WHERE hub=:h AND state IN ('accepted','running','retrying')"), {'h': hub}).scalar_one()
        if pending >= max_pending:
            raise RuntimeError('Webhook backlog is full; retry later')
        now = time.time()
        conn.execute(text("INSERT INTO hz_workflow_events (id,hub,webhook,workflow,digest,payload,state,created,updated,attempt,redrive) VALUES (:i,:h,:w,:f,:d,:p,'accepted',:n,:n,0,0)"), {'i': eid, 'h': hub, 'w': webhook, 'f': workflow, 'd': digest, 'p': json.dumps({**payload, 'key': key, 'webhook': webhook, 'received_at': now}), 'n': now})
        conn.commit()
    return eid


def update(hub_dir, eid, state, *, attempt=None, version=None, error=None, owner=None):
    from . import runtime
    owner = owner or runtime._OWNER
    if owner is not None:
        owner.assert_owned()
    with _engine(hub_dir).begin() as conn:
        conn.execute(text('UPDATE hz_workflow_events SET state=:s,updated=:n,attempt=COALESCE(:a,attempt),version=COALESCE(:v,version),error=:e WHERE id=:i'), {'s': state, 'n': time.time(), 'a': attempt, 'v': version, 'e': error, 'i': eid})


def fail(hub_dir, hub, row, error, *, owner=None):
    """Mark an event failed and raise its alert at once, so the alert loop never
    rescans failed events. One alert source per redrive (`<event>:r<n>`), the
    same id the coordinator run carries, so both paths deduplicate."""
    update(hub_dir, row['id'], 'failed', error=error, owner=owner)
    from .alerts import cooled_record
    source = f"{row['id']}:r{row['redrive']}"
    cooled_record(hub_dir, hub, 'run_failed', source, {'workflow': row['workflow'], 'run_id': source})


def _set_mapping(hub_dir, eid, workflow):
    with _engine(hub_dir).begin() as conn:
        conn.execute(text('UPDATE hz_workflow_events SET workflow=:w WHERE id=:i AND version IS NULL'), {'w':workflow, 'i':eid})


def register(DBOS, hub_dir, hub):
    from . import runtime
    owner = runtime._OWNER   # the engine owner this coordinator belongs to

    @DBOS.workflow(name=COORDINATOR)
    async def coordinator(eid, redrive=0):
        row = await asyncio.to_thread(get, hub_dir, eid)
        if not row or row['state'] in TERMINAL:
            return
        if row['redrive'] != redrive:
            return
        wf = (next((w for w in runtime.registry() if w.on_webhook == row['webhook']), None)
              if row['version'] is None else runtime._REGISTRY.get(row['workflow']))
        if wf is not None and row['version'] is None:
            await asyncio.to_thread(_set_mapping, hub_dir, eid, wf.name)
            row['workflow'] = wf.name
        if wf is None or wf.on_webhook != row['webhook']:
            await asyncio.to_thread(fail, hub_dir, hub, row, 'Workflow mapping changed before execution', owner=owner)
            raise RuntimeError('Webhook workflow mapping is unavailable')
        event = json.loads(row['payload'])
        delays = settings(hub_dir).get('webhook_retry_delays', [60, 300])
        if not isinstance(delays, list) or len(delays) > 9 or any(not isinstance(x, (int,float)) or not math.isfinite(x) or x < 0 for x in delays):
            raise ValueError('webhook_retry_delays must contain at most 9 nonnegative seconds')
        for attempt in range(1, len(delays)+2):
            await asyncio.to_thread(update, hub_dir, eid, 'running', attempt=attempt,
                                    version=runtime._APP_VERSION, owner=owner)
            try:
                options = runtime.enqueue_options(wf, event)
                options.update(workflow_name=wf.name, workflow_id=f'{eid}:r{redrive}:a{attempt}')
                handle = await DBOS.enqueue_workflow_with_options_async(options, hub, {**event, 'attempt': attempt, 'id': eid})
                await handle.get_result()
            except Exception as exc:
                # A cancelled child surfaces as this Exception. Cancelling the
                # coordinator itself raises DBOS's BaseException instead, which
                # passes through; reconcile then fails the event for redrive.
                from dbos._error import DBOSAwaitedWorkflowCancelledError
                if isinstance(exc, DBOSAwaitedWorkflowCancelledError):
                    await asyncio.to_thread(fail, hub_dir, hub, row, 'Cancelled by operator; redrive explicitly to retry', owner=owner)
                    return
                if attempt == len(delays)+1:
                    await asyncio.to_thread(fail, hub_dir, hub, row, type(exc).__name__, owner=owner)
                    raise RuntimeError('Webhook event exhausted its attempts') from exc
                await asyncio.to_thread(update, hub_dir, eid, 'retrying', error=type(exc).__name__, owner=owner)
                await DBOS.sleep_async(float(delays[attempt-1]))
            else:
                await asyncio.to_thread(update, hub_dir, eid, 'succeeded', owner=owner)
                return


def reconcile(hub_dir, hub):
    from . import runtime
    runtime._OWNER.assert_owned()
    with _engine(hub_dir).connect() as conn:
        rows = conn.execute(text("SELECT id,state,version,redrive,workflow FROM hz_workflow_events WHERE hub=:h AND state IN ('accepted','running','retrying') ORDER BY created"), {'h': hub}).mappings().all()
    from dbos import DBOS
    runs = {}
    ids = [f"{row['id']}:r{row['redrive']}" for row in rows]
    for start in range(0, len(ids), 100):
        runs.update({r.workflow_id:r for r in DBOS.list_workflows(workflow_ids=ids[start:start+100], load_input=False, load_output=False, application_name=runtime._app_name(hub))})
    for row in rows:
        rid = f"{row['id']}:r{row['redrive']}"
        run = runs.get(rid)
        old_code = bool(run and run.app_version and run.app_version != runtime._APP_VERSION)
        if old_code and row['version'] is None:
            # No child attempt was ever admitted; dispatch under the current mapping.
            DBOS.cancel_workflow(rid)
            with _engine(hub_dir).begin() as conn:
                conn.execute(text("UPDATE hz_workflow_events SET redrive=redrive+1,updated=:n WHERE id=:i AND version IS NULL AND redrive=:r"),
                             {'i':row['id'], 'r':row['redrive'], 'n':time.time()})
            continue
        if run and (run.status in ('ERROR', 'CANCELLED', 'MAX_RECOVERY_ATTEMPTS_EXCEEDED') or old_code):
            fail(hub_dir, hub, row, 'Coordinator stopped; inspect side effects before redrive')
            continue
        if row['version'] and row['version'] != runtime._APP_VERSION:
            fail(hub_dir, hub, row, 'Code changed during this event; inspect side effects before redrive')
            continue
        if run is not None:
            continue
        DBOS.enqueue_workflow_with_options({'workflow_name': COORDINATOR, 'queue_name': runtime._app_name(hub)+'-events', 'workflow_id': f"{row['id']}:r{row['redrive']}"}, row['id'], row['redrive'])


def redrive(hub_dir, hub, eid):
    """Explicit operator retry; keeps external idempotency key, new attempt IDs."""
    with _engine(hub_dir).begin() as conn:
        changed = conn.execute(text("UPDATE hz_workflow_events SET state='accepted',redrive=redrive+1,attempt=0,version=NULL,error=NULL,updated=:n WHERE id=:i AND hub=:h AND state='failed'"), {'i': eid, 'h': hub, 'n': time.time()}).rowcount
    if not changed:
        raise ValueError('Only a failed event in this hub can be redriven')
