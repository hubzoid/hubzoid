"""Real DBOS coordination across bridge processes, without model calls."""

from __future__ import annotations

import os
import subprocess
import sys
import time

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
        while not all((tmp_path / ("ready-" + w)).exists() for w in ("a", "b")):
            assert time.monotonic() < deadline, "workers did not become ready"
            for p in processes:
                if p.poll() is not None:
                    stdout, stderr = p.communicate()
                    raise AssertionError(
                        f"worker exited before dispatch: {stdout}\n{stderr}"
                    )
            time.sleep(0.05)
        (tmp_path / "go").touch()
        ids = []
        for p in processes:
            stdout, stderr = p.communicate(timeout=45)
            assert p.returncode == 0, stderr
            ids.append(stdout.strip().splitlines()[-1])
        assert ids[0] == ids[1]
        assert effect.read_text() == "executed\n"
    finally:
        for p in processes:
            if p.poll() is None:
                p.kill()
            p.communicate()
