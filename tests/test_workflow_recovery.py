"""A workflow interrupted by a crash resumes on the next start: completed steps
are not repeated and the run finishes once. If the workflow code changed in
between, DBOS does not resume the old run on the new code (a different
application version), so an incompatible change can't silently replay it.

Each phase is its own process because DBOS is a process-global singleton, and a
crash is simulated with SIGKILL.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="uses SIGKILL")

_WORKFLOW = textwrap.dedent('''
    import os, time
    from pathlib import Path
    from hubzoid import workflow, step

    LOG = Path(os.environ["RECOVERY_LOG"])


    @step
    def record(name):
        with LOG.open("a") as f:
            f.write(name + "\\n")


    @step
    def slow_second():
        record_marker = Path(os.environ["RECOVERY_LOG"] + ".in-second")
        if not record_marker.exists():
            record_marker.write_text("1")
            time.sleep(60)          # first attempt: killed here
        with LOG.open("a") as f:
            f.write("second\\n")


    @workflow()
    def nightly():
        record("first")
        slow_second()
        return "finished"
''')

_START = textwrap.dedent('''
    import sys
    from hubzoid.workflows import runtime
    runtime.init(sys.argv[1], hub_name="recovery-hub")
    runtime.load_workflows(sys.argv[1])
    runtime.launch()
    handle = runtime.start("nightly")
    print("STARTED", handle.get_workflow_id(), flush=True)
    import time
    time.sleep(120)
''')

_RESUME = textwrap.dedent('''
    import sys, time
    from dbos import DBOS
    from hubzoid.workflows import runtime
    runtime.init(sys.argv[1], hub_name="recovery-hub")
    runtime.load_workflows(sys.argv[1])
    runtime.launch()
    wid = sys.argv[2]
    deadline = time.time() + 60
    status = None
    while time.time() < deadline:
        status = DBOS.get_workflow_status(wid).status
        if status in ("SUCCESS", "ERROR"):
            break
        time.sleep(0.5)
    print("STATUS", status, flush=True)
    runtime.shutdown()
''')


@pytest.fixture
def hub(tmp_path, monkeypatch):
    wf = tmp_path / "recovery-hub" / "workflows" / "nightly"
    wf.mkdir(parents=True)
    (wf / "main.py").write_text(_WORKFLOW)
    env = dict(os.environ)
    env["HUBZOID_OPERATIONAL_DB"] = f"sqlite:///{tmp_path / 'ops.db'}"
    env["HUBZOID_DBOS_DB"] = f"sqlite:///{tmp_path / 'dbos.db'}"
    env["RECOVERY_LOG"] = str(tmp_path / "steps.log")
    return tmp_path / "recovery-hub", env, tmp_path / "steps.log"


def _crash_mid_run(hub_dir, env, log) -> str:
    proc = subprocess.Popen(
        [sys.executable, "-c", _START, str(hub_dir)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    wid = None
    deadline = time.time() + 90
    while time.time() < deadline:
        line = proc.stdout.readline()
        if line.startswith("STARTED"):
            wid = line.split()[1]
            break
    assert wid, proc.stderr.read()[-2000:] if proc.poll() is not None else "no start"
    marker = log.with_name(log.name + ".in-second")
    while time.time() < deadline and not marker.exists():
        time.sleep(0.2)
    assert marker.exists(), "workflow never reached the second step"
    proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=30)
    return wid


def _resume(hub_dir, env, wid) -> str:
    proc = subprocess.run(
        [sys.executable, "-c", _RESUME, str(hub_dir), wid],
        capture_output=True, text=True, timeout=180, env=env,
    )
    out = [ln for ln in proc.stdout.splitlines() if ln.startswith("STATUS")]
    assert out, proc.stderr[-2000:]
    return out[-1].split()[1]


def test_crashed_run_resumes_and_finishes_once(hub):
    hub_dir, env, log = hub
    wid = _crash_mid_run(hub_dir, env, log)
    assert log.read_text().splitlines() == ["first"]
    assert _resume(hub_dir, env, wid) == "SUCCESS"
    # the completed step was not repeated; the interrupted one ran to completion once
    assert log.read_text().splitlines() == ["first", "second"]


def test_changed_code_does_not_resume_old_run(hub):
    hub_dir, env, log = hub
    wid = _crash_mid_run(hub_dir, env, log)
    main = hub_dir / "workflows" / "nightly" / "main.py"
    main.write_text(main.read_text().replace('return "finished"', 'return "finished v2"'))
    assert _resume(hub_dir, env, wid) == "PENDING"
    assert log.read_text().splitlines() == ["first"]
