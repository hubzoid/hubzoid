"""`hub.call_jev` inside a real DBOS workflow, with OpenRouter replaced by a
local fake: a completed call is checkpointed and not made again when a crashed
run resumes; a failing call fails the step and the run with a readable message
(never an empty answer); the key never reaches a checkpoint or a log.

Each phase is its own process because DBOS is a process-global singleton, and a
crash is simulated with SIGKILL. Live calls are in tests/e2e/test_jev_e2e.py.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="uses SIGKILL")

KEY = "sk-or-v1-dbos-test-SECRET-9876543210"

_WORKFLOW = textwrap.dedent('''
    import os, time
    from pathlib import Path
    from hubzoid import workflow, step, hub

    Q = {"is_billing": {"type": "noul", "instructions": "Is this a billing problem?"}}


    @step
    def hold_after_first_call():
        marker = Path(os.environ["JEV_TEST_DIR"]) / "holding"
        if not marker.exists():
            marker.write_text("1")
            time.sleep(60)          # first attempt: killed here


    @workflow()
    def triage():
        first = hub.call_jev("I was charged twice", Q)
        if os.environ.get("JEV_TEST_HOLD"):
            hold_after_first_call()
        return first["is_billing"]["noul"]
''')

# Every process installs the same fake OpenRouter: it logs each request it gets
# to calls.jsonl, then answers from the REPLY env var (a JSON object with
# "status" and "body").
_BOOT = textwrap.dedent('''
    import json, os, sys, time
    from pathlib import Path
    import httpx
    CALLS = Path(os.environ["JEV_TEST_DIR"]) / "calls.jsonl"

    def fake_post(url, json=None, timeout=None, headers=None):
        with CALLS.open("a") as f:
            f.write(__import__("json").dumps({"url": url, "auth_set": bool(headers.get("Authorization"))}) + "\\n")
        reply = __import__("json").loads(os.environ["REPLY"])
        return httpx.Response(reply["status"], json=reply["body"])

    httpx.post = fake_post
    from dbos import DBOS
    from hubzoid import runtime as agent_rt
    from hubzoid.workflows import context, runtime
    context.configure(jev=lambda spec, hub_dir=None, subject=None:
                      agent_rt.jev_once(hub_dir, spec, subject=subject))
    runtime.init(sys.argv[1], hub_name="jev-hub")
    runtime.load_workflows(sys.argv[1])
    runtime.launch()
''')

_START = _BOOT + textwrap.dedent('''
    handle = runtime.start("triage")
    print("STARTED", handle.get_workflow_id(), flush=True)
    if len(sys.argv) > 2:           # wait for the outcome instead of being killed
        try:
            print("RESULT", handle.get_result(), flush=True)
        except Exception as exc:
            print("FAILED", type(exc).__name__, exc, flush=True)
        runtime.shutdown()
    else:
        time.sleep(120)
''')

_RESUME = _BOOT + textwrap.dedent('''
    wid = sys.argv[2]
    deadline = time.time() + 60
    while time.time() < deadline:
        st = DBOS.get_workflow_status(wid)
        if st.status in ("SUCCESS", "ERROR"):
            break
        time.sleep(0.5)
    print("STATUS", st.status, flush=True)
    print("OUTPUT", json.dumps(DBOS.retrieve_workflow(wid).get_result()), flush=True)
    runtime.shutdown()
''')

OK = {"status": 200, "body": {
    "id": "gen-dec-1", "model": "typesafe/jev-1.13-20260917", "provider": "TypeSafe",
    "answers": {"is_billing": {"type": "noul", "noul": 0.97}},
    "usage": {"input_tokens": 300, "output_tokens": 20, "cost": 0.0000126}}}


@pytest.fixture
def hub(tmp_path):
    wf = tmp_path / "jev-hub" / "workflows" / "triage"
    wf.mkdir(parents=True)
    (wf / "main.py").write_text(_WORKFLOW)
    env = {k: v for k, v in os.environ.items()
           if k not in ("OPENROUTER_API_KEY", "HUBZOID_DEPLOYMENT", "DATABASE_URL")}
    env.update(HUBZOID_OPERATIONAL_DB=f"sqlite:///{tmp_path / 'ops.db'}",
               HUBZOID_DBOS_DB=f"sqlite:///{tmp_path / 'dbos.db'}",
               JEV_TEST_DIR=str(tmp_path), JEV_OPENROUTER_API_KEY=KEY, REPLY=json.dumps(OK))
    return tmp_path / "jev-hub", env, tmp_path


def _calls(tmp_path) -> int:
    f = tmp_path / "calls.jsonl"
    return len(f.read_text().splitlines()) if f.exists() else 0


def _no_key_anywhere(tmp_path, *texts):
    for t in texts:
        assert KEY not in t
    for f in tmp_path.rglob("*"):
        if f.is_file():
            assert KEY.encode() not in f.read_bytes(), f"key found in {f.name}"


def _usage(tmp_path):
    from sqlalchemy import create_engine, text
    eng = create_engine(f"sqlite:///{tmp_path / 'ops.db'}")
    with eng.connect() as c:
        return [dict(r._mapping) for r in c.execute(text("SELECT * FROM hz_usage ORDER BY id"))]


def test_resumed_run_reuses_the_checkpointed_jev_answer(hub):
    hub_dir, env, tmp = hub
    env["JEV_TEST_HOLD"] = "1"
    proc = subprocess.Popen([sys.executable, "-c", _START, str(hub_dir)], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    wid = None
    deadline = time.time() + 90
    while time.time() < deadline and wid is None:
        line = proc.stdout.readline()
        if line.startswith("STARTED"):
            wid = line.split()[1]
    assert wid, proc.stderr.read()[-2000:] if proc.poll() is not None else "no start"
    while time.time() < deadline and not (tmp / "holding").exists():
        time.sleep(0.2)
    assert (tmp / "holding").exists(), "the run never got past the Jev step"
    proc.send_signal(signal.SIGKILL)
    _, first_err = proc.communicate(timeout=30)
    assert _calls(tmp) == 1

    # The provider now answers differently: a repeated call would change the result.
    env["REPLY"] = json.dumps({**OK, "body": {**OK["body"], "answers": {
        "is_billing": {"type": "noul", "noul": 0.01}}}})
    done = subprocess.run([sys.executable, "-c", _RESUME, str(hub_dir), wid], env=env,
                          capture_output=True, text=True, timeout=240)
    lines = done.stdout.splitlines()
    assert "STATUS SUCCESS" in lines, done.stderr[-2000:]
    assert "OUTPUT 0.97" in lines                  # the checkpointed answer, not 0.01
    assert _calls(tmp) == 1                        # the completed call was not made again
    rows = _usage(tmp)
    assert [(r["kind"], r["subject"], r["model"], r["cost_usd"]) for r in rows] == [
        ("jev", "workflow:triage", "typesafe/jev-1.13-20260917", 0.0000126)]
    _no_key_anywhere(tmp, first_err, done.stdout, done.stderr)


@pytest.mark.parametrize("reply,message", [
    ({"status": 401, "body": {"error": {"code": 401, "message": "User not found."}}},
     "OpenRouter rejected JEV_OPENROUTER_API_KEY (HTTP 401): User not found."),
    ({"status": 200, "body": {"model": "m", "answers": {}, "usage": {}}},
     "no noul answer for question 'is_billing'"),
], ids=["auth", "empty-answer"])
def test_a_failed_jev_call_fails_the_step_and_the_run(hub, reply, message):
    hub_dir, env, tmp = hub
    env["REPLY"] = json.dumps(reply)
    proc = subprocess.run([sys.executable, "-c", _START, str(hub_dir), "wait"], env=env,
                          capture_output=True, text=True, timeout=180)
    out = [ln for ln in proc.stdout.splitlines() if ln.startswith(("RESULT", "FAILED"))]
    assert out, proc.stderr[-2000:]
    assert out[-1].startswith("FAILED") and message in out[-1]
    assert _calls(tmp) == 1                        # the step itself adds no retry
    wid = next(ln.split()[1] for ln in proc.stdout.splitlines() if ln.startswith("STARTED"))

    import sqlite3
    db = sqlite3.connect(tmp / "dbos.db")
    status, = db.execute("SELECT status FROM workflow_status WHERE workflow_uuid = ?", (wid,)).fetchone()
    steps = db.execute("SELECT function_name, output, error FROM operation_outputs "
                       "WHERE workflow_uuid = ?", (wid,)).fetchall()
    db.close()
    assert status == "ERROR"
    (name, output, error), = [s for s in steps if s[0].endswith("_jev_step")]
    assert output is None and error                # a failed step, not an empty success
    (row,) = _usage(tmp)
    assert row["status"] == "error"
    _no_key_anywhere(tmp, proc.stdout, proc.stderr)
