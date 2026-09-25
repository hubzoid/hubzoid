"""A workflow's agent call runs the hub's full agent, tools included, so a blind
retry can repeat a message or write the first attempt already made. A failed
call is therefore not retried unless the hub opts in, a failed run is reported
as failed, and `run_once` raises instead of returning the error text.

The DBOS cases run in a subprocess because DBOS is a process-global singleton.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from hubzoid import runtime as runtime_lib

_WORKFLOW = textwrap.dedent('''
    from hubzoid import workflow, hub


    @workflow()
    def flaky():
        return hub.call_agent("send the weekly email")
''')

_SCRIPT = textwrap.dedent('''
    import os, sys, tempfile
    from pathlib import Path
    _d = tempfile.mkdtemp()
    os.environ["HUBZOID_OPERATIONAL_DB"] = f"sqlite:///{_d}/ops.db"
    os.environ["HUBZOID_DBOS_DB"] = f"sqlite:///{_d}/dbos.db"
    from hubzoid.workflows import runtime, context

    calls = []

    def agent(task, **kw):
        calls.append(task)          # stands in for the email the agent sends
        if len(calls) == 1:
            raise RuntimeError("provider timeout after the email was sent")
        return "done"

    context.configure(agent=agent)
    HUB = sys.argv[1]
    runtime.init(HUB, hub_name="retry-hub")
    runtime.load_workflows(HUB)
    runtime.launch()
    try:
        result = runtime.run_now("flaky")
        outcome = f"ok:{result}"
    except Exception as exc:
        outcome = f"failed:{type(exc).__name__}"
    print(f"OUTCOME={outcome} CALLS={len(calls)}")
    runtime.shutdown()
''')


def _run(hub) -> str:
    proc = subprocess.run(
        [sys.executable, "-c", _SCRIPT, str(hub)],
        capture_output=True, text=True, timeout=180,
    )
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("OUTCOME=")]
    assert line, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr[-2000:]}"
    return line[-1]


@pytest.fixture
def hub(tmp_path):
    wf = tmp_path / "retry-hub" / "workflows" / "flaky"
    wf.mkdir(parents=True)
    (wf / "main.py").write_text(_WORKFLOW)
    return tmp_path / "retry-hub"


def test_failed_agent_call_is_not_retried_by_default(hub):
    assert _run(hub) == "OUTCOME=failed:RuntimeError CALLS=1"


def test_hub_can_opt_in_to_agent_retries(hub):
    (hub / "workflows" / "settings.yaml").write_text("agent_max_attempts: 3\n")
    assert _run(hub) == "OUTCOME=ok:done CALLS=2"


class _FailingRuntime:
    last_error = None

    async def aopen(self):
        pass

    async def aclose(self):
        pass

    async def run(self, prompt):
        self.last_error = TimeoutError("model timed out")
        return "\n\n[agent error: TimeoutError: model timed out]"


def test_run_once_raises_instead_of_returning_error_text(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_lib, "build", lambda hub_dir, **kw: _FailingRuntime())
    with pytest.raises(runtime_lib.AgentRunError, match="TimeoutError"):
        runtime_lib.run_once(tmp_path, "do the thing")


class _OkRuntime(_FailingRuntime):
    async def run(self, prompt):
        return "all good"


def test_run_once_returns_text_on_success(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime_lib, "build", lambda hub_dir, **kw: _OkRuntime())
    assert runtime_lib.run_once(tmp_path, "do the thing") == "all good"


_REINIT = textwrap.dedent('''
    import os, sys, tempfile
    _d = tempfile.mkdtemp()
    # one DBOS system database shared by both hubs, as with a shared Postgres
    os.environ["HUBZOID_OPERATIONAL_DB"] = f"sqlite:///{_d}/ops.db"
    os.environ["HUBZOID_DBOS_DB"] = f"sqlite:///{_d}/dbos.db"
    from hubzoid.workflows import runtime, context
    calls = []

    def agent(task, **kw):
        calls.append(task)
        raise RuntimeError("boom")

    context.configure(agent=agent)
    counts = []
    for hub, name in ((sys.argv[1], "hub-a"), (sys.argv[2], "hub-b")):
        calls.clear()
        runtime.init(hub, hub_name=name)
        runtime.load_workflows(hub)
        runtime.launch()
        try:
            runtime.run_now("flaky")
        except Exception:
            pass
        counts.append(len(calls))
        runtime.shutdown()
    print("COUNTS", *counts)
''')


def test_reinit_rebuilds_agent_steps_and_hubs_can_share_a_dbos_db(tmp_path):
    hubs = []
    for name in ("hub-a", "hub-b"):
        wf = tmp_path / name / "workflows" / "flaky"
        wf.mkdir(parents=True)
        (wf / "main.py").write_text(_WORKFLOW)  # identical code in both hubs
        hubs.append(tmp_path / name)
    (hubs[0] / "workflows" / "settings.yaml").write_text("agent_max_attempts: 3\n")
    proc = subprocess.run(
        [sys.executable, "-c", _REINIT, *map(str, hubs)],
        capture_output=True, text=True, timeout=240,
    )
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("COUNTS")]
    assert line, proc.stderr[-2000:]
    assert line[-1] == "COUNTS 3 1"
