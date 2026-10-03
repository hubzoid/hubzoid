"""Real DBOS coordination across bridge processes, without model calls."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.slow  # starts DBOS or another process per test

SCRIPT = """
import sys, time
from pathlib import Path
from hubzoid.workflows import runtime
hub = Path(sys.argv[1])
worker = sys.argv[2]
runtime.init(hub)
runtime.load_workflows(hub)
runtime.launch()
(hub / ('ready-' + worker)).touch()
while not (hub / 'go').exists():
    time.sleep(0.02)
handle = runtime.start('once', scheduled_at='2026-01-01T00:00:00+00:00')
assert handle.get_result() == 'ok'
print(handle.get_workflow_id(), flush=True)
runtime.shutdown()
"""


def test_two_processes_dispatch_one_slot(tmp_path):
    workflow = tmp_path / "workflows" / "once"
    workflow.mkdir(parents=True)
    effect = tmp_path / "effect.txt"
    (workflow / "main.py").write_text(f"""from hubzoid import workflow, step
@step()
def effect():
    with open({str(effect)!r}, 'a') as f:
        f.write('executed\\n')
@workflow()
def once():
    effect()
    return 'ok'
""")
    env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in (
            "HUBZOID_DEPLOYMENT",
            "DATABASE_URL",
            "HUBZOID_DBOS_DB",
            "HUBZOID_OPERATIONAL_DB",
        )
    }
    processes = []
    try:
        # Initialize the schema before the competing bridge launches.
        init = subprocess.run(
            [
                sys.executable,
                "-c",
                "from hubzoid.workflows import runtime; import sys; runtime.init(sys.argv[1]); runtime.launch(); runtime.shutdown()",
                str(tmp_path),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert init.returncode == 0, init.stderr
        for worker in ("a", "b"):
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-c", SCRIPT, str(tmp_path), worker],
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        deadline = time.monotonic() + 30
        while not any((tmp_path / ("ready-" + w)).exists() for w in ("a", "b")):
            assert time.monotonic() < deadline, "owner did not become ready"
            time.sleep(0.05)
        (tmp_path / "go").touch()
        outcomes = []
        for p in processes:
            stdout, stderr = p.communicate(timeout=45)
            outcomes.append((p.returncode, stdout, stderr))
        winners = [(code, stdout, stderr) for code, stdout, stderr in outcomes if code == 0]
        losers = [(code, stdout, stderr) for code, stdout, stderr in outcomes if code != 0]
        assert len(winners) == len(losers) == 1, outcomes
        assert winners[0][1].strip().splitlines()[-1]
        assert "OwnerBusy" in losers[0][2]
        assert effect.read_text() == "executed\n"
    finally:
        for p in processes:
            if p.poll() is None:
                p.kill()
            p.communicate()


def _env():
    return {k: v for k, v in os.environ.items() if k not in
            ("HUBZOID_DEPLOYMENT", "DATABASE_URL", "HUBZOID_DBOS_DB", "HUBZOID_OPERATIONAL_DB")}


def _wait_for(path, proc, seconds=30):
    deadline = time.monotonic() + seconds
    while not path.exists():
        assert proc.poll() is None, proc.communicate()
        assert time.monotonic() < deadline, f"{path.name} never appeared"
        time.sleep(0.05)


BRIDGE = """import sys, time
from hubzoid.workflows import runtime
runtime.init(sys.argv[1]); runtime.load_workflows(sys.argv[1]); runtime.launch()
print(runtime.start(sys.argv[2]).get_workflow_id(), flush=True)
time.sleep(60)
"""

CLI_OWNER = """import sys
from dbos import DBOS
from hubzoid.workflows import runtime
runtime.init(sys.argv[1]); runtime.load_workflows(sys.argv[1]); runtime.launch(target='target')
assert runtime.start('target').get_result() == 'ok'
print(DBOS.list_workflows(workflow_ids=[sys.argv[2]])[0].status, flush=True)
runtime.shutdown()
"""

RESUME = """import sys, time
from dbos import DBOS
from hubzoid.workflows import runtime
runtime.init(sys.argv[1]); runtime.load_workflows(sys.argv[1]); runtime.launch()
deadline = time.monotonic() + 30
while DBOS.list_workflows(workflow_ids=[sys.argv[2]])[0].status != 'SUCCESS':
    assert time.monotonic() < deadline
    time.sleep(0.1)
runtime.shutdown()
"""


def test_temporary_cli_owner_runs_only_its_target(tmp_path):
    """`hubzoid schedule run` with no live bridge owns the hub briefly. DBOS's
    startup recovery puts an interrupted unrelated run back on its own queue;
    the CLI serves only its target's queue, so the bridge runs it later."""
    folder = tmp_path / "workflows" / "jobs"
    folder.mkdir(parents=True)
    effects, started, gate = (tmp_path / n for n in ("effects", "started", "gate"))
    (folder / "main.py").write_text(f"""import time
from pathlib import Path
from hubzoid import workflow
def note(text):
    with open({str(effects)!r}, 'a') as f: f.write(text + '\\n')
@workflow()
def unrelated():
    Path({str(started)!r}).touch()
    while not Path({str(gate)!r}).exists(): time.sleep(0.05)
    note('unrelated')
@workflow()
def target():
    note('target')
    return 'ok'
""")
    bridge = subprocess.Popen([sys.executable, "-c", BRIDGE, str(tmp_path), "unrelated"],
                              env=_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        run_id = bridge.stdout.readline().strip()
        _wait_for(started, bridge)
    finally:
        bridge.kill()
        bridge.communicate()
    gate.touch()   # the interrupted run would now finish wherever it ran
    cli = subprocess.run([sys.executable, "-c", CLI_OWNER, str(tmp_path), run_id], env=_env(),
                         capture_output=True, text=True, timeout=60)
    assert cli.returncode == 0, cli.stderr[-3000:]
    assert cli.stdout.strip() == "ENQUEUED"
    assert effects.read_text() == "target\n"
    resume = subprocess.run([sys.executable, "-c", RESUME, str(tmp_path), run_id], env=_env(),
                            capture_output=True, text=True, timeout=60)
    assert resume.returncode == 0, resume.stderr[-3000:]
    assert effects.read_text() == "target\nunrelated\n"


EVENTS = """import json, sys, time
from pathlib import Path
from dbos import DBOS
from hubzoid.workflows import events, runtime
hub, mode = Path(sys.argv[1]), sys.argv[2]
runtime.init(hub); runtime.load_workflows(hub)
if mode == 'admit-only':
    DBOS.listen_queues(['nothing'])   # accept and plan the event; never start it
runtime.launch()
key = 'E2' if mode == 'start' else 'E1'
if mode != 'upgraded':
    events.admit(hub, hub.name, 'ticket', 'unused', key, {'body': {'id': key}, 'headers': {}})
deadline = time.monotonic() + 30
while time.monotonic() < deadline:
    events.reconcile(hub, hub.name)
    rows = {r['id']: r for r in (events.get(hub, events.event_id(hub.name, 'ticket', k)) for k in ('E1', 'E2')) if r}
    if mode == 'admit-only' and DBOS.list_workflows(name=events.COORDINATOR, status=['ENQUEUED']):
        break
    if mode == 'upgraded' and all(r['state'] in ('succeeded', 'failed') for r in rows.values()):
        print(json.dumps({r['webhook'] + ':' + json.loads(r['payload'])['key']:
                          [r['state'], r['workflow'], r['redrive'], r['error']] for r in rows.values()}), flush=True)
        break
    time.sleep(0.1)
if mode == 'start':
    time.sleep(60)   # killed while E2's first attempt runs
runtime.shutdown()
"""


def test_upgrade_dispatches_unstarted_events_under_new_code_and_fails_started_ones(tmp_path):
    folder = tmp_path / "workflows" / "tickets"
    folder.mkdir(parents=True)
    (tmp_path / "workflows" / "settings.yaml").write_text(
        "webhooks:\n  ticket:\n    verify: header\nwebhook_retry_delays: []\n")
    effects, started = tmp_path / "effects", tmp_path / "started"

    def code(name, label):
        (folder / "main.py").write_text(f"""import time
from pathlib import Path
from hubzoid import workflow
from hubzoid.workflows.context import hub
@workflow(on_webhook='ticket')
def {name}():
    key = hub.event.body['id']
    with open({str(effects)!r}, 'a') as f: f.write('{label}:' + key + '\\n')
    if key == 'E2':
        Path({str(started)!r}).touch()
        time.sleep(60)
""")

    code("handle_v1", "v1")
    first = subprocess.Popen([sys.executable, "-c", EVENTS, str(tmp_path), "start"], env=_env(),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        _wait_for(started, first)
    finally:
        first.kill()
        first.communicate()
    planned = subprocess.run([sys.executable, "-c", EVENTS, str(tmp_path), "admit-only"],
                             env=_env(), capture_output=True, text=True, timeout=60)
    assert planned.returncode == 0, planned.stderr[-3000:]
    code("handle_v2", "v2")   # renamed and changed: a new code version
    upgraded = subprocess.run([sys.executable, "-c", EVENTS, str(tmp_path), "upgraded"],
                              env=_env(), capture_output=True, text=True, timeout=60)
    assert upgraded.returncode == 0, upgraded.stderr[-3000:]
    import json
    rows = json.loads(upgraded.stdout.strip().splitlines()[-1])
    # Never dequeued, so unversioned: the new code picks it up by webhook name.
    assert rows["ticket:E1"][:2] == ["succeeded", "handle_v2"]
    assert rows["ticket:E2"][0] == "failed" and "redrive" in rows["ticket:E2"][3]
    # E2 began under the old code: it is never replayed on the new code.
    assert effects.read_text() == "v1:E2\nv2:E1\n"
