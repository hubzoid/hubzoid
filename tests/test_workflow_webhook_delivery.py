"""Model-free webhook admission and DBOS delivery contracts."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
import time

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from hubzoid.workflows import events, webhooks
from hubzoid.inbound.run import hub_slug


def _client(hub, monkeypatch, spec):
    (hub / "workflows").mkdir(exist_ok=True)
    (hub / "workflows" / "settings.yaml").write_text(
        "webhooks:\n  ticket:\n" + "".join(f"    {k}: {v}\n" for k, v in spec.items())
    )
    monkeypatch.setenv("WEBHOOK_SECRET_TICKET", "test-secret")
    monkeypatch.setattr(webhooks, "readiness", lambda *_: {"webhooks": {"ticket": "handle_ticket"}})
    app = FastAPI()
    app.include_router(webhooks.build_router(hub))
    return TestClient(app), f"/webhooks/{hub_slug(hub)}/ticket"


def test_admission_repeat_and_content_mismatch(tmp_path, monkeypatch):
    client, path = _client(tmp_path, monkeypatch,
                           {"verify": "header", "event_key": '"{payload.id}"'})
    headers = {"X-Hook-Secret": "test-secret", "X-Delivery-Id": "delivery-1",
               "Authorization": "Bearer never-store"}
    body = {"payload": {"id": "T-1", "title": "first"}}
    first = client.post(path, headers=headers, json=body)
    repeat = client.post(path, headers=headers, json=body)
    assert first.status_code == repeat.status_code == 200
    assert first.json() == repeat.json()
    row = events.get(tmp_path, first.json()["event_id"])
    assert row["state"] == "accepted"
    assert json.loads(row["payload"])["headers"] == {"content-type": "application/json",
                                                      "x-delivery-id": "delivery-1"}
    changed = client.post(path, headers=headers,
                          json={"payload": {"id": "T-1", "title": "changed"}})
    assert changed.status_code == 409
    assert events.get(tmp_path, first.json()["event_id"])["digest"] == row["digest"]


def test_hmac_replay_authentication_and_size(tmp_path, monkeypatch):
    client, path = _client(tmp_path, monkeypatch, {"verify": "hmac", "header": "X-Signature",
                                                 "timestamp_header": "X-Timestamp"})
    raw = b'{"payload":{"id":"T-1"}}'
    stamp = str(int(time.time()))
    sig = hmac.new(b"test-secret", stamp.encode() + b"." + raw, hashlib.sha256).hexdigest()
    headers = {"X-Timestamp": stamp, "X-Signature": "sha256=" + sig}
    assert client.post(path, headers=headers, content=raw).status_code == 200
    assert client.post(path, headers=headers, content=raw + b" ").status_code == 401
    old = str(int(time.time()) - 301)
    old_sig = hmac.new(b"test-secret", old.encode() + b"." + raw, hashlib.sha256).hexdigest()
    assert client.post(path, headers={"X-Timestamp": old, "X-Signature": old_sig},
                       content=raw).status_code == 401
    nan_sig = hmac.new(b"test-secret", b"nan." + raw, hashlib.sha256).hexdigest()
    assert client.post(path, headers={"X-Timestamp": "nan", "X-Signature": nan_sig},
                       content=raw).status_code == 401
    assert client.post(path, headers=headers, content=b"x" * (webhooks.MAX_BODY + 1)).status_code == 413


_DBOS_SCRIPT = r'''
import json, os, sys, time
from pathlib import Path
from hubzoid.workflows import runtime, events
hub = Path(sys.argv[1]); mode = sys.argv[2]
runtime.init(hub)
runtime._IDENTITY_STEP = lambda *a: {}
from hubzoid import workflow
from hubzoid.workflows.context import hub as ctx
log = hub / 'calls.jsonl'
@workflow(on_webhook='ticket', concurrency=2, concurrency_key='payload.id',
          timeout=.1 if mode == 'late' else None)
def handle_ticket():
    event = ctx.event
    body = event.body
    key = body['payload']['id']; seq = body['seq']; attempt = event.attempt
    with log.open('a') as f:
        f.write(json.dumps(['enter', key, seq, attempt, time.time()]) + '\n')
    if mode == 'retry' and attempt < 3:
        raise RuntimeError('planned failure')
    if mode == 'late':
        time.sleep(.25)
    if mode == 'concurrent':
        time.sleep(.5)
    with log.open('a') as f:
        f.write(json.dumps(['exit', key, seq, attempt, time.time()]) + '\n')
runtime.launch()
if mode != 'recover':
    for key, seq in ([('T-1', 1)] if mode in ('retry', 'admit-only', 'late') else [('T-1', 1), ('T-1', 2), ('T-2', 1)]):
        body = {'payload': {'id': key}, 'seq': seq}
        events.admit(hub, hub.name, 'ticket', 'handle_ticket', f'{key}:{seq}', {'body': body, 'headers': {}})
if mode == 'admit-only':
    runtime.shutdown(); sys.exit(0)
deadline = time.monotonic() + 20
expected = 1 if mode in ('retry', 'recover', 'late') else 3
while time.monotonic() < deadline:
    events.reconcile(hub, hub.name)
    with events._engine(hub).connect() as conn:
        from sqlalchemy import text
        rows = conn.execute(text('SELECT state FROM hz_workflow_events WHERE hub=:h'), {'h': hub.name}).all()
    if len(rows) == expected and all(row.state == 'succeeded' for row in rows):
        print('DELIVERED', flush=True); runtime.shutdown(); sys.exit(0)
    time.sleep(.1)
print('TIMED_OUT', [row.state for row in rows], flush=True)
runtime.shutdown(); sys.exit(1)
'''


def _run_dbos(hub, mode):
    env = {k: v for k, v in os.environ.items() if k not in
           ("DATABASE_URL", "HUBZOID_DBOS_DB", "HUBZOID_OPERATIONAL_DB", "HUBZOID_DEPLOYMENT")}
    proc = subprocess.run([sys.executable, "-c", _DBOS_SCRIPT, str(hub), mode], env=env,
                          text=True, capture_output=True, timeout=35)
    assert proc.returncode == 0, proc.stdout + "\n" + proc.stderr[-3000:]
    return proc


@pytest.mark.slow
def test_dbos_retry_and_partition_exclusion(tmp_path):
    (tmp_path / "workflows").mkdir()
    (tmp_path / "workflows" / "settings.yaml").write_text(
        "webhooks:\n  ticket:\n    verify: header\nwebhook_retry_delays: [0, 0]\n"
    )
    _run_dbos(tmp_path, "retry")
    calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert [call[3] for call in calls if call[0] == "enter"] == [1, 2, 3]


@pytest.mark.slow
def test_dbos_different_ticket_keys_parallel_same_key_serial(tmp_path):
    (tmp_path / "workflows").mkdir()
    (tmp_path / "workflows" / "settings.yaml").write_text(
        "webhooks:\n  ticket:\n    verify: header\nwebhook_retry_delays: []\n"
    )
    _run_dbos(tmp_path, "concurrent")
    calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    one = sorted([(c[4], 1 if c[0] == "enter" else -1) for c in calls if c[1] == "T-1"])
    active = 0
    for _, delta in one:
        active += delta
        assert active <= 1
    assert len([c for c in calls if c[0] == "enter"]) == 3
    intervals = {}
    for action, key, seq, _, when in calls:
        intervals.setdefault((key, seq), {})[action] = when
    other = intervals[("T-2", 1)]
    assert any(max(other["enter"], times["enter"]) < min(other["exit"], times["exit"])
               for (key, _), times in intervals.items() if key == "T-1")


@pytest.mark.slow
def test_admitted_event_survives_process_restart(tmp_path):
    (tmp_path / "workflows").mkdir()
    (tmp_path / "workflows" / "settings.yaml").write_text(
        "webhooks:\n  ticket:\n    verify: header\nwebhook_retry_delays: []\n"
    )
    _run_dbos(tmp_path, "admit-only")
    assert events.get(tmp_path, events.event_id(tmp_path.name, "ticket", "T-1:1"))["state"] == "accepted"
    _run_dbos(tmp_path, "recover")


@pytest.mark.slow
def test_successful_late_side_effect_is_not_retried(tmp_path):
    (tmp_path / "workflows").mkdir()
    (tmp_path / "workflows" / "settings.yaml").write_text(
        "webhooks:\n  ticket:\n    verify: header\nwebhook_retry_delays: [0]\n"
    )
    _run_dbos(tmp_path, "late")
    calls = [json.loads(line) for line in (tmp_path / "calls.jsonl").read_text().splitlines()]
    assert [call[3] for call in calls if call[0] == "enter"] == [1]
    assert [call[0] for call in calls] == ["enter", "exit"]


@pytest.mark.parametrize("settings, env", [
    ("webhooks:\n  bad-name:\n    verify: hmac\n", ""),      # no signed timestamp header
    ("webhooks: [unclosed\n", ""),                             # malformed YAML
    ("webhooks:\n  whatsapp:\n    verify: header\n", ""),     # the chat surface's route
    ("webhooks:\n  squadcast:\n    verify: header\n",         # the legacy webhook's route
     "WEBHOOK_INBOUND_NAME=squadcast\n"),
])
def test_invalid_workflow_settings_leave_gateway_and_chat_routes_available(tmp_path, monkeypatch,
                                                                         settings, env):
    import asyncio
    from hubzoid import gateway
    from hubzoid.access import store_for
    from hubzoid.workflows import boot

    monkeypatch.delenv("HUBZOID_DEPLOYMENT", raising=False)
    monkeypatch.delenv("WEBHOOK_INBOUND_NAME", raising=False)
    (tmp_path / "AGENTS.md").write_text(
        "---\nname: test-hub\ndescription: Test hub\n---\nHelp the team.\n"
    )
    (tmp_path / ".env").write_text("BRIDGE_PORT=4611\nMODEL=hubzoid-test/scripted\n" + env)
    (tmp_path / "workflows").mkdir()
    (tmp_path / "workflows" / "settings.yaml").write_text(settings)
    plan = gateway.plan([tmp_path])
    paths = {route["prefix"] for route in plan.edge_routes(web_app=True)}
    assert f"/b/{plan.backends[0].slug}/api" in paths
    assert not any(path.startswith("/webhooks/") for path in paths)
    assert asyncio.run(boot.start(tmp_path)) is None
    health = store_for(tmp_path).runtime_health(tmp_path.name)
    assert health["enabled"] is False and "invalid workflow settings" in health["error"]


_COORDINATOR_BACKLOG_SCRIPT = r'''
import os, sys, threading, time
from pathlib import Path
from hubzoid.workflows import events, runtime
hub = Path(sys.argv[1])
gate = threading.Event()
runtime.init(hub)
runtime._IDENTITY_STEP = lambda *a: {}
@runtime.workflow(on_webhook='slow', concurrency=1)
def slow():
    gate.wait(20)
@runtime.workflow(on_webhook='fast')
def fast():
    return 'fast-ok'
runtime.launch()
for number in range(33):
    events.admit(hub, hub.name, 'slow', 'slow', str(number),
                 {'body': {'number': number}, 'headers': {}})
fast_id = events.admit(hub, hub.name, 'fast', 'fast', 'fast-1',
                       {'body': {'number': 'fast'}, 'headers': {}})
events.reconcile(hub, hub.name)
deadline = time.monotonic() + 12
while time.monotonic() < deadline:
    if events.get(hub, fast_id)['state'] == 'succeeded':
        break
    time.sleep(.1)
else:
    gate.set()
    runtime.shutdown(completion_timeout_sec=0)
    raise AssertionError('fast workflow starved behind 33 waiting coordinators')
assert events.get(hub, events.event_id(hub.name, 'slow', '0'))['state'] != 'succeeded'
gate.set()
runtime.shutdown(completion_timeout_sec=0)
print('FAST_BEFORE_SLOW', flush=True)
'''


@pytest.mark.slow
def test_more_than_32_waiting_coordinators_do_not_starve_other_workflow(tmp_path):
    (tmp_path / "workflows").mkdir()
    (tmp_path / "workflows" / "settings.yaml").write_text(
        "webhooks:\n  slow:\n    verify: header\n  fast:\n    verify: header\n"
        "webhook_retry_delays: []\n"
    )
    env = {k: v for k, v in os.environ.items() if k not in
           ("DATABASE_URL", "HUBZOID_DBOS_DB", "HUBZOID_OPERATIONAL_DB", "HUBZOID_DEPLOYMENT")}
    proc = subprocess.run([sys.executable, "-c", _COORDINATOR_BACKLOG_SCRIPT, str(tmp_path)],
                          env=env, text=True, capture_output=True, timeout=45)
    assert proc.returncode == 0 and "FAST_BEFORE_SLOW" in proc.stdout, (
        proc.stdout + "\n" + proc.stderr[-3000:]
    )


_CANCEL_SCRIPT = r'''
import sys, time
from pathlib import Path
from dbos import DBOS
from hubzoid.workflows import events, runtime
hub = Path(sys.argv[1])
runtime.init(hub)
runtime._IDENTITY_STEP = lambda *a: {}
@runtime.workflow(on_webhook='ticket')
def handle():
    (hub / 'started').touch()
    while True:
        DBOS.sleep(0.1)   # a cancelled run stops at its next DBOS step
runtime.launch()
eid = events.admit(hub, hub.name, 'ticket', 'handle', 'T-1', {'body': {'id': 'T-1'}, 'headers': {}})
deadline = time.monotonic() + 20
while not (hub / 'started').exists():
    events.reconcile(hub, hub.name); time.sleep(0.1)
    assert time.monotonic() < deadline
DBOS.cancel_workflow(eid + ':r0:a1')   # what the Console's cancel does
while events.get(hub, eid)['state'] != 'failed':
    events.reconcile(hub, hub.name); time.sleep(0.1)
    assert time.monotonic() < deadline
time.sleep(1.5)   # a retry would have been queued by now
attempts = [w.workflow_id for w in DBOS.list_workflows(workflow_id_prefix=eid + ':r0:a')]
print(events.get(hub, eid)['error'], sorted(attempts), flush=True)
runtime.shutdown(completion_timeout_sec=0)
'''


@pytest.mark.slow
def test_operator_cancel_fails_the_event_for_explicit_redrive(tmp_path):
    (tmp_path / "workflows").mkdir()
    (tmp_path / "workflows" / "settings.yaml").write_text(
        "webhooks:\n  ticket:\n    verify: header\nwebhook_retry_delays: [0, 0]\n")
    env = {k: v for k, v in os.environ.items() if k not in
           ("DATABASE_URL", "HUBZOID_DBOS_DB", "HUBZOID_OPERATIONAL_DB", "HUBZOID_DEPLOYMENT")}
    proc = subprocess.run([sys.executable, "-c", _CANCEL_SCRIPT, str(tmp_path)], env=env,
                          text=True, capture_output=True, timeout=45)
    assert proc.returncode == 0, proc.stdout + proc.stderr[-3000:]
    out = proc.stdout.strip().splitlines()[-1]
    assert out.startswith("Cancelled by operator; redrive explicitly")
    assert out.endswith(":r0:a1']")   # no second attempt despite retries being allowed


_BOOT_SCRIPT = r'''
import asyncio, sys, time
from pathlib import Path
from hubzoid.access import store_for
from hubzoid.workflows import boot, monitor, runtime
from hubzoid.workflows.ownership import Owner
hub, mode = Path(sys.argv[1]), sys.argv[2]
boot.OWNER_RETRY_SECONDS = 0.2

async def main():
    if mode == 'broken':
        assert await boot.start(hub) is None
        health = store_for(hub).runtime_health(hub.name)
        print('HEALTH', health['enabled'], health['error'], flush=True)
        print('MONITOR', monitor.inspect(hub), flush=True)
        return
    cli = Owner(hub, hub.name).acquire()          # `hubzoid schedule run` owns the hub
    waiting = await boot.start(hub)
    await asyncio.sleep(0.6)                       # a few refused attempts
    assert waiting.dispatcher is None and not runtime._INITED
    await waiting.stop()                           # bridge shutdown while waiting
    assert not runtime._INITED
    supervisor = await boot.start(hub)
    cli.close()                                    # the CLI run ends
    deadline = time.monotonic() + 10
    while supervisor.dispatcher is None:
        assert time.monotonic() < deadline, 'never took ownership'
        await asyncio.sleep(0.1)
    assert runtime._LAUNCHED
    await supervisor.stop()
    assert not runtime._INITED
    print('OK', flush=True)

asyncio.run(main())
'''


@pytest.mark.slow
def test_engine_waits_for_a_temporary_owner_and_reports_broken_modules(tmp_path):
    env = {k: v for k, v in os.environ.items() if k not in
           ("DATABASE_URL", "HUBZOID_DBOS_DB", "HUBZOID_OPERATIONAL_DB", "HUBZOID_DEPLOYMENT",
            "HUBZOID_SCHEDULES", "HUBZOID_GATEWAY")}
    folder = tmp_path / "workflows" / "tickets"
    folder.mkdir(parents=True)
    (tmp_path / "workflows" / "settings.yaml").write_text("webhooks:\n  ticket:\n    verify: header\n")
    (folder / "main.py").write_text("from hubzoid import workflow\n"
                                    "@workflow(on_webhook='ticket')\ndef handle():\n    return 1\n")
    proc = subprocess.run([sys.executable, "-c", _BOOT_SCRIPT, str(tmp_path), "retry"], env=env,
                          text=True, capture_output=True, timeout=60)
    assert proc.returncode == 0 and "OK" in proc.stdout, proc.stdout + proc.stderr[-3000:]
    # A webhook-only hub whose module cannot load keeps the error in health.
    (folder / "main.py").write_text("def broken(:\n")
    proc = subprocess.run([sys.executable, "-c", _BOOT_SCRIPT, str(tmp_path), "broken"], env=env,
                          text=True, capture_output=True, timeout=60)
    assert proc.returncode == 0, proc.stdout + proc.stderr[-3000:]
    assert "HEALTH False" in proc.stdout and "SyntaxError" in proc.stdout
    assert "MONITOR False" in proc.stdout
