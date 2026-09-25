"""Code workflows: one run at a time per workflow, different workflows side by
side, and an optional hub-wide cap. DBOS runs in a subprocess (it is a
process-global singleton)."""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

_WORKFLOWS = textwrap.dedent('''
    import os, time
    from pathlib import Path
    from hubzoid import workflow

    LOG = Path(os.environ["CONC_LOG"])


    def _span(name):
        with LOG.open("a") as f:
            f.write(f"start {name} {time.time()}\\n")
        time.sleep(1.5)
        with LOG.open("a") as f:
            f.write(f"end {name} {time.time()}\\n")


    @workflow()
    def alpha():
        _span("alpha")


    @workflow()
    def beta():
        _span("beta")
''')

_SCRIPT = textwrap.dedent('''
    import sys
    from hubzoid.workflows import runtime
    runtime.init(sys.argv[1], hub_name="conc")
    runtime.load_workflows(sys.argv[1])
    runtime.launch()
    handles = [runtime.start(n) for n in sys.argv[2].split(",")]
    for h in handles:
        h.get_result()
    runtime.shutdown()
    print("DONE")
''')


def _overlaps(log_text):
    """The most runs that were in flight at the same moment."""
    order = []
    for line in log_text.splitlines():
        kind, name, ts = line.split()
        order.append((float(ts), kind, name))
    order.sort()
    running, max_running = 0, 0
    for _, kind, _ in order:
        running += 1 if kind == "start" else -1
        max_running = max(max_running, running)
    return max_running


@pytest.fixture
def hub(tmp_path):
    wf = tmp_path / "conc" / "workflows" / "pair"
    wf.mkdir(parents=True)
    (wf / "main.py").write_text(_WORKFLOWS)
    return tmp_path / "conc"


def _run(hub, tmp_path, names):
    log = tmp_path / "spans.log"
    env = {
        **os.environ,
        "CONC_LOG": str(log),
        "HUBZOID_OPERATIONAL_DB": f"sqlite:///{tmp_path / 'ops.db'}",
        "HUBZOID_DBOS_DB": f"sqlite:///{tmp_path / 'dbos.db'}",
    }
    proc = subprocess.run([sys.executable, "-c", _SCRIPT, str(hub), names],
                          capture_output=True, text=True, timeout=180, env=env)
    assert "DONE" in proc.stdout, proc.stderr[-2000:]
    return log.read_text()


def test_different_workflows_run_side_by_side(hub, tmp_path):
    assert _overlaps(_run(hub, tmp_path, "alpha,beta")) == 2


def test_same_workflow_never_overlaps(hub, tmp_path):
    assert _overlaps(_run(hub, tmp_path, "alpha,alpha")) == 1


def test_hub_cap_limits_all_workflows(hub, tmp_path):
    (hub / "workflows" / "settings.yaml").write_text("max_concurrent_workflows: 1\n")
    assert _overlaps(_run(hub, tmp_path, "alpha,beta")) == 1
