"""A workflow interrupted by a crash resumes on the next start: completed steps
are not repeated and the run finishes once. If the workflow code changed in
between, the old run is not resumed on the new code (a different application
version); it is cancelled so it can't hold the hub's single queue slot, and new
runs still go through.

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
    if len(sys.argv) > 3:  # then prove the queue is not blocked
        new = runtime.start("nightly", scheduled_at="after-change")
        deadline = time.time() + 60
        while time.time() < deadline:
            s = DBOS.get_workflow_status(new.get_workflow_id()).status
            if s in ("SUCCESS", "ERROR"):
                break
            time.sleep(0.5)
        print("NEW", s, flush=True)
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


def _resume(hub_dir, env, wid, *, then_new_run=False):
    args = [sys.executable, "-c", _RESUME, str(hub_dir), wid]
    if then_new_run:
        args.append("new")
    proc = subprocess.run(args, capture_output=True, text=True, timeout=240, env=env)
    lines = proc.stdout.splitlines()
    out = [ln for ln in lines if ln.startswith("STATUS")]
    assert out, proc.stderr[-2000:]
    status = out[-1].split()[1]
    if not then_new_run:
        return status
    new = [ln for ln in lines if ln.startswith("NEW")]
    assert new, proc.stderr[-2000:]
    return status, new[-1].split()[1]


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
    old_status, new_status = _resume(hub_dir, env, wid, then_new_run=True)
    assert old_status == "CANCELLED"  # not replayed on the new code
    assert new_status == "SUCCESS"  # and it doesn't block the queue
    lines = log.read_text().splitlines()
    assert lines[0] == "first" and lines.count("first") == 2  # only the new run ran
