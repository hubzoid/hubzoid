"""Opt-in alert outbox. Importing this module never sends a message.

Only the running product's configured sender calls deliver(). Tests inject a
local sender. Destinations are configured by the deployment operator.
"""
from __future__ import annotations
import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from urllib.parse import quote
from sqlalchemy import text
from . import events

log = logging.getLogger(__name__)
DELIVERY = 'hz_alert_delivery'


def _env(hub_dir, key):
    from pathlib import Path
    from ..config_secrets import resolve_key
    value, _ = resolve_key(Path(hub_dir), key)
    return value if value is not None else os.environ.get(key, '')


def destinations(hub_dir, workflow=None):
    from . import runtime
    try:
        cfg = events.settings(hub_dir).get('alerts', {})
    except Exception:
        cfg = {}
    wf = runtime._REGISTRY.get(workflow)
    selected = wf.alert_to if wf and wf.alert_to is not None else cfg.get('to', [])
    if workflow and workflow.startswith('md:'):
        from ..scheduling import load_tasks
        task = next((t for t in load_tasks(hub_dir)[0] if t.name == workflow[3:]), None)
        if task and task.alert_to is not None:
            selected = task.alert_to
    if wf and wf.on_failure and wf.alert_to is None:
        selected = [{'webhook': wf.on_failure}]
    if not selected and _env(hub_dir, 'HUBZOID_ALERT_URL'):
        selected = [{'webhook': 'HUBZOID_ALERT_URL'}]
    return selected or []


def record(hub_dir, hub, kind, source, payload):
    """Persist one row per occurrence/destination, without sending anything."""
    from ..evals.calls import redact
    now = time.time()
    base = os.environ.get('HUBZOID_PUBLIC_URL', '').rstrip('/')
    # Explicit allowlist: no workflow outputs or raw exceptions in messages.
    data = {k:payload[k] for k in ('workflow','run_id','count') if k in payload}
    link = base+'/portal/#/agents/'+quote(hub,safe='')+'/runs'
    if payload.get('workflow') and payload.get('run_id'):
        link += '/'+quote(payload['workflow'],safe='')+'/'+quote(payload['run_id'],safe='')
    data.update(hub=hub, kind=kind, url=link)
    data = redact(data)
    targets = destinations(hub_dir, payload.get('workflow')) or [{'unconfigured': True}]
    with events._engine(hub_dir).begin() as conn:
        for target in targets:
            aid = 'alert:' + hashlib.sha256(json.dumps([hub,kind,source,target],sort_keys=True).encode()).hexdigest()
            conn.execute(text("INSERT INTO hz_workflow_alerts (id,hub,kind,payload,destination,state,attempt,due,created) VALUES (:i,:h,:k,:p,:d,'pending',0,:n,:n) ON CONFLICT (id) DO NOTHING"), {'i':aid,'h':hub,'k':kind,'p':json.dumps(data),'d':json.dumps(target),'n':now})


def _row(hub_dir, aid):
    with events._engine(hub_dir).connect() as conn:
        row = conn.execute(text('SELECT * FROM hz_workflow_alerts WHERE id=:i'), {'i':aid}).mappings().first()
    return dict(row) if row else None


async def send(hub_dir, row):
    """Product runtime adapter; never invoked during build or import."""
    import httpx
    target, payload = json.loads(row['destination']), json.loads(row['payload'])
    if 'unconfigured' in target:
        raise ValueError('No alert destination configured')
    if 'email' in target:
        await asyncio.to_thread(_email, hub_dir, row, target['email'], payload)
        return
    key = 'slack' if 'slack' in target else 'webhook'
    ref = target.get(key, '')
    url = ref if ref.startswith(('https://','http://')) else _env(hub_dir, ref)
    if not url:
        raise ValueError('Alert destination environment variable is missing')
    content = {'text': f"Hubzoid {payload['kind']}: {payload.get('workflow',payload['hub'])}\n{payload['url']}"} if key == 'slack' else payload
    body = json.dumps(content,separators=(',',':')).encode()
    stamp = str(int(time.time()))
    headers = {'Content-Type':'application/json','Idempotency-Key':row['id'],'X-Hubzoid-Timestamp':stamp}
    secret = _env(hub_dir, 'HUBZOID_ALERT_SECRET')
    if secret and key == 'webhook':
        headers['X-Hubzoid-Signature'] = 'sha256='+hmac.new(secret.encode(),stamp.encode()+b'.'+body,hashlib.sha256).hexdigest()
    async with asyncio.timeout(15):
        async with httpx.AsyncClient(timeout=10,follow_redirects=False) as client:
            response = await client.post(url,content=body,headers=headers)
            response.raise_for_status()


def _email(hub_dir, row, recipient, payload):
    from .. import email_delivery
    from email.message import EmailMessage
    import smtplib
    cfg = email_delivery.config()
    problem = email_delivery.config_problem(cfg) or email_delivery.recipient_problem(recipient)
    if problem:
        raise ValueError(problem)
    msg = EmailMessage()
    msg['From'],msg['To'] = cfg.sender,recipient
    msg['Subject'] = f"Hubzoid: {payload['kind']} in {payload['hub']}"
    msg['Message-ID'] = '<'+row['id'].replace(':','-')+'@hubzoid.local>'
    msg.set_content(json.dumps(payload,indent=2))
    if cfg.mode == 'preview':
        from pathlib import Path
        directory = Path(hub_dir)/'.hubzoid'/'alert-preview'
        directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        fd = os.open(directory/(row['id'].split(':')[-1]+'.eml'),os.O_CREAT|os.O_WRONLY|os.O_TRUNC,0o600)
        with os.fdopen(fd,'wb') as stream:
            stream.write(msg.as_bytes())
    else:
        email_delivery._smtp_send(cfg,msg,recipient,lambda:None,None)


def recover_deliveries(hub_dir):
    # A killed sender cannot retain its claim forever. The fixed receiver key
    # handles an ambiguous send before the process died (at-least-once).
    with events._engine(hub_dir).begin() as conn:
        conn.execute(text("UPDATE hz_workflow_alerts SET state=CASE WHEN attempt>=5 THEN 'failed' ELSE 'pending' END,error='Delivery interrupted' WHERE state='sending' AND due<=:n"), {'n':time.time()})


def _claim(hub_dir, aid, prior):
    with events._engine(hub_dir).begin() as conn:
        return conn.execute(text("UPDATE hz_workflow_alerts SET state='sending',attempt=:a,due=:d WHERE id=:i AND state='pending' AND attempt=:prior"),
            {'a':prior+1, 'd':time.time()+300, 'i':aid, 'prior':prior}).rowcount


def _finish(hub_dir, aid, attempt, state, due, error):
    with events._engine(hub_dir).begin() as conn:
        conn.execute(text("UPDATE hz_workflow_alerts SET state=:s,due=:d,error=:e WHERE id=:i AND state='sending' AND attempt=:a"),
            {'s':state, 'a':attempt, 'd':due, 'e':error, 'i':aid})


async def deliver(hub_dir, aid, *, sender=None):
    # Database work runs in threads: the edge calls this on its event loop.
    row = await asyncio.to_thread(_row, hub_dir, aid)
    if not row or row['state'] != 'pending':
        return
    attempt = row['attempt']+1
    if not await asyncio.to_thread(_claim, hub_dir, aid, row['attempt']):
        return
    try:
        await (sender or send)(hub_dir, row)
    except Exception as exc:
        state = 'failed' if attempt >= 5 else 'pending'
        due, error = time.time()+min(3600,30*2**attempt), type(exc).__name__
        log.warning('Alert %s delivery failed (%s)', aid, error)
    else:
        state, due, error = 'sent', time.time(), None
    await asyncio.to_thread(_finish, hub_dir, aid, attempt, state, due, error)


def register(DBOS,hub_dir):
    @DBOS.workflow(name=DELIVERY)
    async def delivery(aid):
        await deliver(hub_dir,aid)


def dispatch(hub_dir,hub):
    recover_deliveries(hub_dir)
    from dbos import DBOS
    from . import runtime
    with events._engine(hub_dir).connect() as conn:
        rows = conn.execute(text("SELECT id,attempt FROM hz_workflow_alerts WHERE hub=:h AND state='pending' AND due<=:n AND kind NOT IN ('engine_stale','engine_recovered') ORDER BY due LIMIT 100"),{'h':hub,'n':time.time()}).mappings().all()
    for row in rows:
        DBOS.enqueue_workflow_with_options({'workflow_name':DELIVERY,'queue_name':runtime._app_name(hub)+'-alerts','workflow_id':row['id']+':'+str(row['attempt'])+':'+runtime._APP_VERSION},row['id'])


# Incremental scan of finished runs, by DBOS completion time (not creation, so a
# long run that finishes late is still seen). A durable cursor survives
# restarts and outages of any length. Commits can land slightly out of
# timestamp order, so each pass re-reads the last OVERLAP and skips the run ids
# it already handled there. A first start looks back FIRST_LOOKBACK only.
TERMINAL_RUNS = ['SUCCESS', 'ERROR', 'CANCELLED', 'MAX_RECOVERY_ATTEMPTS_EXCEEDED']
OVERLAP_MS = 5 * 60_000
WINDOW_MS = 3600_000          # one query reads at most an hour of completions
WINDOWS_PER_PASS = 24         # a long outage catches up over a few passes
FIRST_LOOKBACK_MS = 24 * 3600_000


def _iso(ms):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def _run_name(hub_dir, run):
    from . import markdown
    rid = run.workflow_id
    if run.name == markdown.MD_WORKFLOW and markdown.task_name_from_id(rid):
        return 'md:' + markdown.task_name_from_id(rid)
    if run.name == events.COORDINATOR:
        event = events.get(hub_dir, rid.split(':r')[0])
        return event['workflow'] if event else run.name
    return run.name


def _scheduled(run):
    """Only scheduled outcomes count toward pausing a schedule: not manual,
    Console or webhook runs, and not legacy markdown webhook (`events-`) runs."""
    from . import markdown
    if run.name == markdown.MD_WORKFLOW:
        return markdown.is_scheduled_slot(markdown.slot_from_id(run.workflow_id))
    return (getattr(run, 'attributes', None) or {}).get('trigger') == 'schedule'


def reconcile(hub_dir, hub):
    from dbos import DBOS
    from . import runtime
    from ..access import store_for
    from .state import WorkflowState
    from .deadlines import seconds
    runtime._OWNER.assert_owned()
    overdue()
    cfg = events.settings(hub_dir).get('alerts', {})
    threshold = int(cfg.get('failures_in_a_row', 3))
    pause = int(cfg.get('pause_after_failures', 20))
    cooldown = seconds(cfg.get('cooldown', '1h'))
    state = WorkflowState(events._engine(hub_dir), hub, '__alerts__')
    now_ms = time.time() * 1000
    cursor = state.get('cursor') or {'t': now_ms - FIRST_LOOKBACK_MS, 'recent': {}}
    t, recent = cursor['t'], dict(cursor['recent'])
    gs = store_for(hub_dir)
    lo = t - OVERLAP_MS
    for _ in range(WINDOWS_PER_PASS):
        hi = lo + WINDOW_MS
        last = hi >= now_ms
        rows = DBOS.list_workflows(status=TERMINAL_RUNS, completed_after=_iso(lo),
                                   completed_before=None if last else _iso(hi),
                                   load_input=False, load_output=False,
                                   application_name=runtime._app_name(hub))
        rows = [r for r in rows if r.completed_at is not None]
        fresh = sorted((r for r in rows if r.workflow_id not in recent),
                       key=lambda r: (r.completed_at, r.workflow_id))
        streaks = {}
        for run in fresh:
            _observe(hub_dir, hub, run, state, gs, streaks, threshold, pause, cooldown)
        for name, value in streaks.items():
            state['streak:'+name] = value
        recent.update({r.workflow_id: r.completed_at for r in rows if _counted(r)})
        t = max([t, *(r.completed_at for r in rows)] + ([hi] if not last else []))
        recent = {rid: at for rid, at in recent.items() if at >= t - OVERLAP_MS}
        state['cursor'] = {'t': t, 'recent': recent}
        if last:
            break
        lo = hi
    check_schedules(hub_dir, hub, state)
    dispatch(hub_dir, hub)


def _counted(run):
    """Alert deliveries and webhook attempts (their event counts once) are
    ignored, and an operator cancellation neither fails nor resets a streak."""
    rid = run.workflow_id
    return not (run.name == DELIVERY or (rid.startswith('wh:') and ':a' in rid)
                or run.status == 'CANCELLED')


def _observe(hub_dir, hub, run, state, gs, streaks, threshold, pause, cooldown):
    """One finished run, in completion order: failure alerts, the failures-in-a-
    row episode, and the scheduled streak that pauses a failing schedule."""
    from . import markdown
    if not _counted(run):
        return
    rid = run.workflow_id
    name = _run_name(hub_dir, run)
    failed = run.status in ('ERROR', 'MAX_RECOVERY_ATTEMPTS_EXCEEDED')
    streak = streaks.get(name) or state.get('streak:'+name) or {'n': 0, 'episode': None, 'scheduled': 0}
    if failed:
        if not streak['n']:
            streak['episode'] = rid
        streak['n'] += 1
        kind = 'eval_failed' if run.name == markdown.EVAL_WORKFLOW else 'run_failed'
        cooled_record(hub_dir, hub, kind, rid, {'workflow': name, 'run_id': rid}, cooldown=cooldown)
        if threshold > 0 and streak['n'] >= threshold:
            # One alert per episode: the id is derived from its first failure.
            record(hub_dir, hub, 'consecutive_failures', streak['episode'],
                   {'workflow': name, 'count': streak['n']})
    else:
        streak['n'] = 0
    if _scheduled(run):
        streak['scheduled'] = streak.get('scheduled', 0) + 1 if failed else 0
        if (failed and pause > 0 and streak['scheduled'] >= pause
                and state.get('paused:'+name) != rid and name not in gs.paused_workflows(hub)):
            from .control import set_paused, ControlError
            try:
                set_paused(hub_dir, name, True, actor='workflow-monitor', surface='workflow',
                           prefer='markdown' if name.startswith('md:') else 'code')
            except ControlError as exc:
                if exc.code != 'unknown':
                    raise   # 'unknown': history may name a deleted schedule
            else:
                state['paused:'+name] = rid
                record(hub_dir, hub, 'schedule_paused', rid,
                       {'workflow': name, 'count': streak['scheduled']})
    streaks[name] = streak


def cooled_record(hub_dir, hub, kind, source, payload, *, cooldown=None):
    from .state import WorkflowState
    from .deadlines import seconds
    state = WorkflowState(events._engine(hub_dir), hub, '__alerts__')
    seen = 'seen:'+kind+':'+source
    if state.get(seen):
        return
    rule = 'cooldown:'+str(payload.get('workflow'))+':'+kind
    previous = state.get(rule, {})
    if time.time() >= previous.get('until', 0):
        record(hub_dir, hub, kind, source, {**payload, 'count':previous.get('suppressed',0)+1})
        period = cooldown if cooldown is not None else seconds(events.settings(hub_dir).get('alerts',{}).get('cooldown','1h'))
        state[rule] = {'until':time.time()+period, 'suppressed':0}
    else:
        state[rule] = {**previous, 'suppressed':previous.get('suppressed',0)+1}
    state[seen] = True


def check_schedules(hub_dir, hub, state):
    from datetime import datetime, timezone
    from . import runtime
    from .schedule_grammar import next_after
    from ..access import store_for
    from .boot import schedules_enabled
    if not schedules_enabled():
        return
    gs = store_for(hub_dir)
    held = gs.schedule_hold()
    paused = gs.paused_workflows(hub)
    for wf in runtime.registry():
        if not wf.schedule:
            continue
        if held or wf.name in paused:
            state['dispatch:'+wf.name] = time.time()
            continue
        key = 'dispatch:'+wf.name
        last = state.get(key)
        from dbos import DBOS
        started = DBOS.list_workflows(name=wf.name, attributes={'trigger':'schedule'},
            status=['PENDING','SUCCESS','ERROR','MAX_RECOVERY_ATTEMPTS_EXCEEDED'],
            limit=1, sort_desc=True, load_input=False, load_output=False, application_name=runtime._app_name(hub))
        if started and started[0].dequeued_at:
            last = max(last or 0, started[0].dequeued_at/1000)
            state[key] = last
        if last is None:
            state[key] = time.time()
            continue
        first = next_after(wf.schedule, wf.timezone, datetime.fromtimestamp(last, timezone.utc))
        second = next_after(wf.schedule, wf.timezone, first)
        if time.time() > second.timestamp()+300:
            cooled_record(hub_dir, hub, 'schedule_stopped', wf.name+':'+str(last), {'workflow':wf.name})


# Only observes deadlines; it never cancels a thread or releases its queue slot.
from contextlib import contextmanager
from threading import Lock
_ACTIVE = {}
_ACTIVE_LOCK = Lock()


@contextmanager
def track_run(hub_dir, hub, workflow, run_id, deadline):
    with _ACTIVE_LOCK:
        _ACTIVE[run_id] = (hub_dir, hub, workflow, deadline)
    try:
        yield
    finally:
        try:
            if deadline is not None and time.time() > deadline:
                cooled_record(hub_dir, hub, 'run_timeout', run_id, {'workflow':workflow, 'run_id':run_id})
        except Exception:
            log.exception('Could not record overdue run')
        finally:
            with _ACTIVE_LOCK:
                _ACTIVE.pop(run_id, None)


def overdue():
    with _ACTIVE_LOCK:
        runs = list(_ACTIVE.items())
    for rid, (directory, hub, workflow, deadline) in runs:
        if deadline is not None and time.time() > deadline:
            cooled_record(directory, hub, 'run_timeout', rid, {'workflow': workflow, 'run_id': rid})
